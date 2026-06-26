# QIF (Quicken Interchange Format) parser (ADR-043; cash sections added later).
#
# Parses the sections a Banktivity / Quicken export carries:
#   !Account        — account name / type / cleared balance
#   !Type:Cat       — the source category list (parsed, but not bulk-created in
#                     round 1; see ADR-043)
#   !Type:Invst     — investment transactions (Buy / Sell / Div / Cash / …)
#   !Type:Bank / !Type:CCard / !Type:Cash / !Type:Oth A|L
#                   — cash-ledger transactions (date / amount / payee / category)
#   !Type:Security  — the securities master (name / ticker / type)
#
# Returns a QifFile whose `.transactions` are normalised dicts that are a
# SUPERSET of the cash dict the import service already consumes from
# csv_parser / ofx_parser — same fitid/date/amount/tx_type/payee_raw/memo/
# status_override/category_raw keys, PLUS investment extras (action /
# security_name / quantity / price / commission / linked_account). The service
# therefore reuses its existing sign-from-tx_type, duplicate-detection, and
# category-resolution machinery unchanged.
#
# The crux is the action → cash-sign mapping. The existing `txn.amount` column
# stays the SIGNED CASH IMPACT, so cash balance = SUM(amount) is correct:
#   Buy                       cash out   (tx_type debit)
#   Sell                      cash in    (tx_type credit)
#   Div / CGShort / CGLong /
#     IntInc / MiscInc / CGMid  cash in  (credit)  — distribution received
#   Cash                      signed T   (debit if T<0 else credit)
#   ShrsIn / ShrsOut /
#     ReinvDiv / StkSplit       zero cash (a reinvested dividend nets to zero)
#   XIn  / XOut               cash ±T (in / out)
# Quantity is always the positive magnitude QIF exports; the action carries
# direction. Unknown actions fall back to "T as a signed cash amount" so a
# real cash movement is never silently dropped (logged at WARNING).
#
# Cash-ledger sections (!Type:Bank / !Type:CCard / !Type:Cash / !Type:Oth) are
# normalised into the same cash dict the service already consumes from CSV/OFX;
# splits aren't exploded into child rows yet (the row imports at its total).

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

# Reuse the shared currency-string parser (strips £/$/€ + thousands commas).
from mfl_desktop.import_engine.csv_parser import _decode, _parse_amount_str

logger = logging.getLogger(__name__)


# ── Action classification ────────────────────────────────────────────────────

# The action sets are shared with the holdings engine (ADR-044) — single source
# of truth in qif_actions so the parser's cash-sign mapping and the holdings
# engine's share-direction mapping can never drift apart.
from mfl_desktop.import_engine.qif_actions import (  # noqa: E402
    SHARE_IN_ACTIONS as _SHARE_IN_ACTIONS,
    SHARE_OUT_ACTIONS as _SHARE_OUT_ACTIONS,
    CASH_IN_ACTIONS as _CASH_IN_ACTIONS,
    ZERO_CASH_ACTIONS as _ZERO_CASH_ACTIONS,
)

# Income actions → the existing system category, resolved by the import
# service via find_or_create_category_path(["Income", "Investment income"]).
_INCOME_CATEGORY = "Income:Investment income"


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class QifAccount:
    name: str = ""
    type: str = ""                       # QIF account type, e.g. 'Invst'
    balance: Optional[Decimal] = None    # `B` line — cleared balance at export


@dataclass
class QifSecurity:
    name: str
    symbol: str = ""
    type: str = ""                       # QIF `T` in the Security section


@dataclass
class QifCategory:
    name: str                            # may be a 'Parent:Child' path
    kind: str = "expense"                # 'income' | 'expense'


@dataclass
class QifFile:
    account: QifAccount = field(default_factory=QifAccount)
    securities: list[QifSecurity] = field(default_factory=list)
    categories: list[QifCategory] = field(default_factory=list)
    transactions: list[dict] = field(default_factory=list)
    is_investment: bool = False          # True once a !Type:Invst section seen


# ── Public entry point ────────────────────────────────────────────────────────


def parse_qif(file_bytes: bytes, filename: str = "") -> QifFile:
    """Parse QIF file bytes into a QifFile.

    Raises ValueError if no recognised section is found (so the import
    service can surface a clean 'not a QIF we understand' message).
    """
    content = _decode(file_bytes)
    result = QifFile()

    section = ""                         # 'account' | 'cat' | 'invst' | 'security' | ''
    record: list[tuple[str, str]] = []   # (code, value) pairs of the current record
    saw_known_section = False

    def flush() -> None:
        if record:
            _dispatch_record(section, list(record), result)
        record.clear()

    for raw_line in content.splitlines():
        line = raw_line.rstrip("\r\n")
        if not line:
            continue

        if line.startswith("!"):
            flush()
            section = _section_for_header(line)
            if section in ("invst", "cat", "security", "account", "cash"):
                saw_known_section = True
            if section == "invst":
                result.is_investment = True
            continue

        if line.startswith("^"):
            flush()
            continue

        # A field line: first character is the QIF code, the rest is the value.
        code, value = line[0], line[1:].strip()
        record.append((code, value))

    flush()  # trailing record with no closing '^'

    if not saw_known_section:
        raise ValueError(
            "This file doesn't look like a QIF export (no recognised "
            "!Account / !Type: section was found)."
        )

    logger.info(
        "QIF parse: %d transactions, %d securities, %d categories "
        "(account=%r, investment=%s) from %s",
        len(result.transactions), len(result.securities),
        len(result.categories), result.account.name,
        result.is_investment, filename or "file",
    )
    return result


# ── Section dispatch ──────────────────────────────────────────────────────────


def _section_for_header(header: str) -> str:
    h = header.strip().lower()
    if h.startswith("!account"):
        return "account"
    if h.startswith("!type:cat"):
        return "cat"
    if h.startswith("!type:invst"):
        return "invst"
    if h.startswith("!type:security"):
        return "security"
    # Cash-ledger transactions: bank accounts, credit cards, cash, and the
    # generic asset/liability ledgers Banktivity/Quicken export (Oth A / Oth L).
    # All share the same record shape (D/T/C/P/M/L), parsed as cash (ADR-043 amd).
    if (
        h.startswith("!type:bank")
        or h.startswith("!type:ccard")
        or h.startswith("!type:cash")
        or h.startswith("!type:oth")
    ):
        return "cash"
    # !Option / !Clear / !Type:Memorized / !Type:Prices / etc. — skipped.
    return "ignore"


def _dispatch_record(section: str, fields: list[tuple[str, str]], result: QifFile) -> None:
    if section == "account":
        _apply_account_record(fields, result)
    elif section == "security":
        _apply_security_record(fields, result)
    elif section == "cat":
        _apply_category_record(fields, result)
    elif section == "invst":
        txn = _normalise_invst_record(fields)
        if txn is not None:
            result.transactions.append(txn)
    elif section == "cash":
        txn = _normalise_cash_record(fields)
        if txn is not None:
            result.transactions.append(txn)
    # 'ignore' / '' → drop the record.


def _apply_account_record(fields: list[tuple[str, str]], result: QifFile) -> None:
    """Merge an !Account record into result.account (last-wins for non-empty
    fields; balance is kept from whichever record actually carries a `B`)."""
    acct = result.account
    for code, value in fields:
        if code == "N" and value:
            acct.name = value
        elif code == "T" and value:
            acct.type = value
        elif code == "B":
            parsed = _parse_amount_str(value)
            if parsed is not None:
                acct.balance = parsed


def _apply_security_record(fields: list[tuple[str, str]], result: QifFile) -> None:
    name = symbol = sec_type = ""
    for code, value in fields:
        if code == "N":
            name = value
        elif code == "S":
            symbol = value
        elif code == "T":
            sec_type = value
    if name:
        result.securities.append(QifSecurity(name=name, symbol=symbol, type=sec_type))


def _apply_category_record(fields: list[tuple[str, str]], result: QifFile) -> None:
    name = ""
    kind = "expense"
    for code, value in fields:
        if code == "N":
            name = value
        elif code == "I":            # bare 'I' line marks an income category
            kind = "income"
        elif code == "E":
            kind = "expense"
    if name:
        result.categories.append(QifCategory(name=name, kind=kind))


# ── Investment transaction normalisation ──────────────────────────────────────


def _normalise_invst_record(fields: list[tuple[str, str]]) -> Optional[dict]:
    date_raw = action = security = payee = memo = ""
    cleared = ""
    linked_account = ""
    price = quantity = total = commission = None

    for code, value in fields:
        if code == "D":
            date_raw = value
        elif code == "N":
            action = value
        elif code == "Y":
            security = value
        elif code == "I":
            price = _parse_amount_str(value)
        elif code == "Q":
            quantity = _parse_amount_str(value)
        elif code == "T":
            total = _parse_amount_str(value)
        elif code == "O":
            commission = _parse_amount_str(value)
        elif code == "C":
            cleared = value.strip().lower()
        elif code == "P":
            payee = value
        elif code == "M":
            memo = value
        elif code == "L":
            inner = _bracketed(value)
            if inner is not None:
                linked_account = inner      # L[Account] — a transfer's other side

    date_iso = _parse_qif_date(date_raw)
    if not date_iso:
        logger.warning("Skipping QIF investment row with unparseable date: %r", date_raw)
        return None

    amount, tx_type = _cash_impact(action, total)

    # Memo: source free text, plus a note of the linked account for transfer
    # rows (round 1 imports these as plain cash; real linking is a later round).
    #
    # ADR-071: the brokerage `P` field on an investment row is a *description*
    # (e.g. "DIV - SABRA HEALTH CARE REIT INC REC 08/29/25 …"), not a payee —
    # the security (`Y`) already carries the "who". Earlier rounds copied it
    # into payee_raw, so every re-import of an investment QIF regenerated a
    # wall of junk payees. We now fold it into the memo and leave payee_raw
    # empty (the service's get_or_create_payee("") returns None), so investment
    # rows never mint a payee. De-duped against `M` so a P==M source doesn't
    # double up; investment dedup hashes on action+security+quantity, not
    # payee, so dropping it from payee_raw doesn't disturb re-import matching.
    memo_parts: list[str] = []
    for part in (payee, memo):
        part = part.strip()
        if part and part not in memo_parts:
            memo_parts.append(part)
    if linked_account:
        direction = "from" if tx_type == "credit" else "to"
        memo_parts.append(f"Transfer {direction} {linked_account}")
    full_memo = " | ".join(memo_parts)

    # Income distributions get the system Investment-income category; everything
    # else is left to Uncategorised (buys/sells are asset moves, not spend).
    category_raw = _INCOME_CATEGORY if action.strip().lower() in _CASH_IN_ACTIONS else ""

    status_override = "Cleared" if cleared in ("c", "x", "r") else ""

    return {
        "fitid": "",                      # QIF has no FITID; service hashes instead
        "date": date_iso,
        "amount": amount,                 # positive magnitude; tx_type carries sign
        "tx_type": tx_type,
        "payee_raw": "",                  # ADR-071: P folded into memo, not a payee
        "memo": full_memo,
        "status_override": status_override,
        "category_raw": category_raw,
        # Investment extras (consumed by the import service's commit path):
        "action": action.strip(),
        "security_name": security,
        "quantity": quantity,             # Decimal magnitude or None
        "price": price,                   # Decimal or None
        "commission": commission,         # Decimal or None
        "linked_account": linked_account,
    }


def _normalise_cash_record(fields: list[tuple[str, str]]) -> Optional[dict]:
    """Normalise a QIF cash-ledger record (Bank / CCard / Cash / Oth) into the
    same cash dict the import service consumes from CSV/OFX (ADR-043 amendment).

    Fields: ``D`` date, ``T``/``U`` signed amount, ``C`` cleared flag, ``P``
    payee, ``M`` memo, ``N`` cheque/reference number, ``L`` category — or
    ``L[Account]`` for a transfer, imported as plain cash with a memo note (real
    transfer linking is a later round, mirroring the investment path). Split
    lines (``S``/``E``/``$``) are not exploded into child rows in this round; the
    record imports at its total ``T`` and, lacking an ``L``, falls back to the
    first split category so it isn't silently uncategorised."""
    date_raw = payee = memo = number = ""
    cleared = ""
    linked_account = ""
    category = ""
    total: Optional[Decimal] = None
    split_categories: list[str] = []

    for code, value in fields:
        if code == "D":
            date_raw = value
        elif code in ("T", "U"):
            # T and U are the same signed amount in practice; first non-None wins.
            if total is None:
                total = _parse_amount_str(value)
        elif code == "C":
            cleared = value.strip().lower()
        elif code == "P":
            payee = value
        elif code == "M":
            memo = value
        elif code == "N":
            number = value.strip()
        elif code == "L":
            inner = _bracketed(value)
            if inner is not None:
                linked_account = inner          # L[Account] — transfer's other side
            else:
                category = value
        elif code == "S":
            inner = _bracketed(value)
            if inner is None and value.strip():
                split_categories.append(value)

    date_iso = _parse_qif_date(date_raw)
    if not date_iso:
        logger.warning("Skipping QIF cash row with unparseable date: %r", date_raw)
        return None

    t = total if total is not None else Decimal("0")
    amount = abs(t)
    tx_type = "debit" if t < 0 else "credit"

    memo_parts: list[str] = []
    if number:
        memo_parts.append(f"#{number}")
    if memo.strip():
        memo_parts.append(memo.strip())
    if linked_account:
        direction = "from" if tx_type == "credit" else "to"
        memo_parts.append(f"Transfer {direction} {linked_account}")
    full_memo = " | ".join(memo_parts)

    # Prefer the L category; for a split row (no L) fall back to its first split
    # category so the whole amount isn't dumped into Uncategorised. A transfer
    # (L[Account]) leaves the category empty — it's cash for now, not a spend.
    category_raw = ""
    if not linked_account:
        category_raw = category or (split_categories[0] if split_categories else "")

    status_override = "Cleared" if cleared in ("c", "x", "r") else ""

    return {
        "fitid": "",                          # QIF has no FITID; service hashes
        "date": date_iso,
        "amount": amount,                     # positive magnitude; tx_type signs it
        "tx_type": tx_type,
        "payee_raw": payee.strip(),
        "memo": full_memo,
        "status_override": status_override,
        "category_raw": category_raw,
    }


def _cash_impact(action: str, total: Optional[Decimal]) -> tuple[Decimal, str]:
    """Return (positive magnitude, tx_type) for a QIF action + total.

    tx_type is 'debit' (cash out) or 'credit' (cash in); the service signs the
    stored amount from it, exactly like the cash CSV/OFX path. Share-only
    actions return (0, 'credit')."""
    a = action.strip().lower()
    t = total if total is not None else Decimal("0")

    if a in _ZERO_CASH_ACTIONS:
        return Decimal("0"), "credit"
    if a in {"buy", "buyx", "cvrshrt"}:
        return abs(t), "debit"
    if a in {"sell", "sellx", "shtsell"}:
        return abs(t), "credit"
    if a in _CASH_IN_ACTIONS:
        return abs(t), "credit"
    if a in {"cash", "xin", "contribx", "contrib"}:
        # T already carries its sign (deposit + / withdrawal −).
        return abs(t), ("debit" if t < 0 else "credit")
    if a in {"xout", "withdrwx", "withdraw"}:
        return abs(t), "debit"

    # Unknown action: trust the signed total so real cash is never dropped.
    if total is not None:
        logger.warning(
            "Unrecognised QIF action %r — treating total %s as signed cash.",
            action, total,
        )
    return abs(t), ("debit" if t < 0 else "credit")


# ── Helpers ────────────────────────────────────────────────────────────────────


def _bracketed(value: str) -> Optional[str]:
    """'[Chase Checking]' → 'Chase Checking'. Returns None if not bracketed
    (a plain category reference, which round 1 ignores)."""
    v = value.strip()
    if v.startswith("[") and v.endswith("]"):
        return v[1:-1].strip()
    return None


def _parse_qif_date(date_str: str) -> str:
    """Parse a QIF date into ISO 'YYYY-MM-DD'.

    QIF uses US M/D/Y ordering. Banktivity exports M/D/YY (e.g. '2/6/21').
    Quicken sometimes writes the 2000s with an apostrophe ("2/6'21") and may
    pad with spaces — both are handled. Two-digit years map to 2000-2099.
    """
    s = date_str.strip().replace("'", "/")
    parts = re.split(r"[/\-.]", s)
    if len(parts) != 3:
        return ""
    try:
        month, day, year = (int(p.strip()) for p in parts)
    except ValueError:
        return ""
    if year < 100:
        year += 2000
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"
