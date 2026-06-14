"""QAbstractTableModel for the register, backed by the Repository.

Loads the account's transactions into memory at construction time (Banktivity-
style — the dataset is personal-finance scale, tens of thousands at most).
Inline edits route through the Repository, then update the in-memory row so
the view repaints from the same source of truth.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from mfl_desktop.db.repository import Repository, TransactionRow

# Custom Qt role for "give me the underlying ID, not the display string."
# Used by the category delegate to read the current category_id from a cell.
ID_ROLE = Qt.UserRole + 1


class TransactionTableModel(QAbstractTableModel):
    """Backs the register table. Two column layouts:

    - Single-account (account_id is set): Date / Payee / Category / Status /
      Memo / Amount / Balance.
    - All-transactions (account_id is None): Date / Account / Payee /
      Category / Status / Memo / Amount  — no Balance, because it isn't
      meaningful across accounts of different types and currencies.
    - Investment account (invest=True): Date / Action / Symbol / Security /
      Qty / Price / Status / Memo / Amount / Balance (ADR-043). The
      security-action columns are read-only in round 1; Status/Memo stay
      editable. Amount is read-only here because it is the derived cash
      impact of the action. Symbol sits next to Security because security
      names are often near-identical and differ between statement and
      exchange — the ticker is the disambiguator for manual entry.
    """

    # (header_label, attribute_name, editable)
    COLUMNS_SINGLE = [
        ("Date",     "posted_date",     True),
        ("Payee",    "payee_name",      True),
        ("Category", "category_name",   True),
        ("Status",   "status",          True),
        ("Memo",     "memo",            True),
        ("Amount",   "amount",          True),
        ("Balance",  "running_balance", False),
    ]
    COLUMNS_ALL = [
        ("Date",     "posted_date",     True),
        ("Account",  "account_name",    False),
        ("Payee",    "payee_name",      True),
        ("Category", "category_name",   True),
        ("Status",   "status",          True),
        ("Memo",     "memo",            True),
        ("Amount",   "amount",          True),
    ]
    # ADR-048: investment rows are edited through the InvestmentTransactionDialog
    # (double-click the row), not inline — so every column is non-editable here.
    # That also frees the table's double-click to open the dialog instead of an
    # inline editor.
    COLUMNS_INVEST = [
        ("Date",     "posted_date",     False),
        ("Action",   "action",          False),
        ("Symbol",   "security_symbol", False),
        ("Security", "security_name",   False),
        ("Qty",      "quantity",        False),
        ("Price",    "price",           False),
        ("Status",   "status",          False),
        ("Memo",     "memo",            False),
        ("Amount",   "amount",          False),
        ("Balance",  "running_balance", False),
    ]

    def __init__(
        self, repo: Repository, account_id: int | None, since: str | None = None,
        invest: bool = False,
    ) -> None:
        super().__init__()
        self._repo = repo
        self._account_id = account_id
        # ADR-041: inclusive 'YYYY-MM-DD' lower bound on posted_date, or None
        # for the full history. The window is pushed into the Repository query
        # (not the proxy) so load + reset + sort all shrink to what's shown.
        self._since = since
        # ADR-043: investment accounts use a security-aware column layout.
        if invest and account_id is not None:
            self.COLUMNS = self.COLUMNS_INVEST
        elif account_id is not None:
            self.COLUMNS = self.COLUMNS_SINGLE
        else:
            self.COLUMNS = self.COLUMNS_ALL
        self._rows: list[TransactionRow] = []
        # ADR-061: a lowercased free-text haystack per row, built once on load
        # (and refreshed on inline edit) so the proxy's filterAcceptsRow does a
        # single substring test per keystroke instead of rebuilding the seven
        # source fields + two amount formats for every row on every keystroke.
        # Kept index-parallel with self._rows.
        self._search_blobs: list[str] = []
        # Optional gate (ADR-040): set by the window to a callable
        # ``(txn_id) -> bool``. When a reconciled row is about to be edited
        # inline, the model asks the gate; a False answer rejects the edit.
        # Left None (no gate) means edits always proceed — keeps the model
        # usable headless / in tests without a UI confirm.
        self.reconciled_edit_guard = None

    def reload(self) -> None:
        self.beginResetModel()
        if self._account_id is not None:
            self._rows = self._repo.list_transactions_for_account(
                self._account_id, since=self._since,
            )
        else:
            self._rows = self._repo.list_all_transactions(since=self._since)
        self._search_blobs = [self._build_search_blob(r) for r in self._rows]
        self.endResetModel()

    @staticmethod
    def _build_search_blob(row: TransactionRow) -> str:
        """The free-text haystack the proxy searches (ADR-061). Both the signed
        and absolute amount forms are included — and the amount formats are
        comma-free (``f"{x:.2f}"``) — so a needle with commas stripped (see
        ``TransactionFilterProxy.set_search``) matches either direction."""
        return " ".join(filter(None, [
            row.payee_name,
            row.category_name,     # ADR-061 amend: search by category too
            row.memo,
            row.posted_date,
            row.security_symbol,   # investment rows: search by ticker…
            row.security_name,     # …and by security name
            f"{row.amount:.2f}",
            f"{abs(row.amount):.2f}",
        ])).lower()

    def search_blob_at(self, source_row: int) -> str:
        return self._search_blobs[source_row]

    def set_since(self, since: str | None) -> None:
        """Change the date window (ADR-041) and reload in place. The column
        layout is unchanged, so the window can re-window an existing model
        without the delegate/​column-width teardown that swapping models does."""
        self._since = since
        self.reload()

    def row_at(self, source_row: int) -> TransactionRow:
        return self._rows[source_row]

    # ── required overrides ──

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole or orientation != Qt.Horizontal:
            return None
        return self.COLUMNS[section][0]

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        if not index.isValid():
            return Qt.NoItemFlags
        f = Qt.ItemIsSelectable | Qt.ItemIsEnabled
        if self.COLUMNS[index.column()][2]:
            # Split transactions (ADR-051) are edited through the split dialog
            # (double-click the row), not inline — the parent total and the
            # per-line categories have to change together to keep the sum
            # invariant. So the whole split row is non-editable inline, the
            # same way investment rows are (which also frees the double-click
            # to open the dialog instead of an inline editor).
            row = self._rows[index.row()]
            if row.split_count:
                return f
            f |= Qt.ItemIsEditable
        return f

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col_name = self.COLUMNS[index.column()][1]

        # Underlying ID — used by delegates that pick by id (category, status).
        if role == ID_ROLE:
            if col_name == "category_name":
                return row.category_id
            if col_name == "status":
                return row.status
            return None

        if role in (Qt.DisplayRole, Qt.EditRole):
            value = getattr(row, col_name)
            # A split transaction (ADR-051) has no single category — its lines
            # carry the categories. Show the Banktivity-style "—Split—" marker.
            if (
                col_name == "category_name"
                and row.split_count
                and role == Qt.DisplayRole
            ):
                return "—Split—"
            if col_name in ("amount", "running_balance"):
                return f"{value:,.2f}"
            if col_name == "quantity":
                return _fmt_shares(value)
            if col_name == "price":
                return _fmt_price(value)
            return value or ""

        if role == Qt.TextAlignmentRole:
            if col_name in ("amount", "running_balance", "quantity", "price"):
                return int(Qt.AlignRight | Qt.AlignVCenter)
            return int(Qt.AlignLeft | Qt.AlignVCenter)

        if role == Qt.ForegroundRole and col_name == "amount":
            return QColor("#1b8a3a") if row.amount >= 0 else QColor("#b3261e")

        return None

    def setData(self, index: QModelIndex, value, role=Qt.EditRole) -> bool:
        if not index.isValid() or role != Qt.EditRole:
            return False
        col_name = self.COLUMNS[index.column()][1]
        if not self.COLUMNS[index.column()][2]:
            return False

        row = self._rows[index.row()]
        if (
            self.reconciled_edit_guard is not None
            and self._repo.is_reconciled(row.id)
            and not self.reconciled_edit_guard(row.id)
        ):
            return False
        updated = self._apply_edit(row, col_name, value)
        if updated is None:
            return False
        self._rows[index.row()] = updated
        self._search_blobs[index.row()] = self._build_search_blob(updated)
        self.dataChanged.emit(index, index, [Qt.DisplayRole, Qt.EditRole])
        return True

    # ── edit routing ──

    def _apply_edit(
        self, row: TransactionRow, col_name: str, value,
    ) -> Optional[TransactionRow]:
        # Defensive: split rows are dialog-edited (ADR-051); flags() already
        # blocks inline edits, so this only fires on a programmatic push.
        if row.split_count:
            return None
        if col_name == "posted_date":
            try:
                stored = self._repo.update_transaction_date(row.id, str(value))
            except ValueError:
                return None
            return replace(row, posted_date=stored)

        if col_name == "payee_name":
            new_name = str(value).strip()
            payee_id, display = self._repo.update_transaction_payee(row.id, new_name)
            return replace(row, payee_id=payee_id, payee_name=display)

        if col_name == "category_name":
            category_id = int(value)
            new_name = self._repo.update_transaction_category(row.id, category_id)
            return replace(row, category_id=category_id, category_name=new_name)

        if col_name == "status":
            new_status = str(value)
            self._repo.update_transaction_status(row.id, new_status)
            return replace(row, status=new_status)

        if col_name == "memo":
            new_memo = str(value)
            self._repo.update_transaction_memo(row.id, new_memo)
            return replace(row, memo=new_memo)

        if col_name == "amount":
            parsed = _parse_amount_input(str(value))
            if parsed is None:
                return None
            # Repository handles transfer-half sign coercion + partner sync;
            # the returned value is what actually landed on this row, which
            # may differ in sign from the user's input for transfer rows.
            try:
                stored = self._repo.update_transaction_amount(row.id, parsed)
            except ValueError:
                return None
            return replace(row, amount=stored)

        return None


def _fmt_shares(value) -> str:
    """Format a share quantity (ADR-043): up to 6 dp, trailing zeros trimmed,
    so 180.0 → '180' and 0.069 → '0.069'. Blank for None."""
    if value is None:
        return ""
    s = f"{float(value):,.6f}".rstrip("0").rstrip(".")
    return s or "0"


def _fmt_price(value) -> str:
    """Format a per-share price (ADR-043): thousands-separated, 2-4 dp with
    trailing zeros beyond 2 dp trimmed (67.07 → '67.07', 104.194 → '104.194').
    Blank for None."""
    if value is None:
        return ""
    s = f"{float(value):,.4f}"
    if "." in s:
        whole, frac = s.split(".")
        frac = frac.rstrip("0")
        if len(frac) < 2:
            frac = (frac + "00")[:2]
        s = f"{whole}.{frac}"
    return s


def _parse_amount_input(text: str) -> Optional[Decimal]:
    """Parse a user-typed amount string into a signed Decimal.

    Lenient — strips common currency symbols (£/$/€), commas (so the
    display format "1,234.56" round-trips when the user clicks an
    already-formatted cell), and whitespace. Returns None on empty
    input or unparseable text; the model treats that as "edit
    rejected, leave the cell alone."
    """
    s = (
        text.strip()
        .replace("£", "").replace("$", "").replace("€", "")
        .replace(",", "")
        .strip()
    )
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None
