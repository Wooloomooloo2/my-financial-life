# OFX and QFX file parser using ofxtools.
#
# Parses OFX 1.x (SGML), OFX 2.x (XML), and QFX (Quicken variant).
# Returns a normalised list of transaction dicts regardless of format.
#
# Amount convention: always positive Decimal, direction from tx_type.
# OFX signed amounts: negative = debit (money out), positive = credit (in).

from __future__ import annotations

import logging
from decimal import Decimal
from io import BytesIO
from typing import Optional

logger = logging.getLogger(__name__)


def parse_ofx(file_bytes: bytes, filename: str = "") -> list[dict]:
    """
    Parse OFX or QFX file bytes into a list of normalised transaction dicts.

    Each dict contains:
        fitid         (str)     — bank-assigned unique ID (used as import hash)
        date          (str)     — ISO date "YYYY-MM-DD"
        amount        (Decimal) — always positive
        tx_type       (str)     — "debit" or "credit"
        payee_raw     (str)     — payee name from file
        memo          (str)     — memo/description from file
        status_override (str)   — always "" for OFX
        category_raw  (str)     — always "" for OFX

    Raises:
        ValueError if the file cannot be parsed as OFX/QFX.
    """
    try:
        from ofxtools.Parser import OFXTree
    except ImportError:
        raise ImportError("ofxtools is required for OFX import. Run: pip install ofxtools")

    try:
        parser = OFXTree()
        parser.parse(BytesIO(file_bytes))
        ofx = parser.convert()
    except Exception as e:
        raise ValueError(f"Could not parse file as OFX/QFX: {e}")

    transactions = []
    statement_count = 0

    for stmt in ofx.statements:
        statement_count += 1
        raw_txns = getattr(stmt, "transactions", []) or []

        for txn in raw_txns:
            try:
                txn_dict = _normalise_transaction(txn)
                if txn_dict:
                    transactions.append(txn_dict)
            except Exception as e:
                logger.warning(f"Skipping unparseable transaction: {e}")
                continue

    if statement_count == 0:
        raise ValueError(
            "No bank statements found in this OFX file. "
            "Ensure the file is a valid bank statement export."
        )

    logger.info(
        f"Parsed {len(transactions)} transactions from "
        f"{statement_count} statement(s) in {filename or 'file'}"
    )
    return transactions


def _normalise_transaction(txn) -> Optional[dict]:
    """Normalise a single ofxtools transaction object to a plain dict."""
    raw_amount = getattr(txn, "trnamt", None)
    if raw_amount is None:
        return None

    trnamt = Decimal(str(raw_amount))
    if trnamt < 0:
        amount = abs(trnamt)
        tx_type = "debit"
    else:
        amount = trnamt
        tx_type = "credit"

    dtposted = getattr(txn, "dtposted", None)
    if dtposted is None:
        return None
    if hasattr(dtposted, "date"):
        date_iso = dtposted.date().isoformat()
    else:
        date_iso = str(dtposted)[:10]

    fitid = str(getattr(txn, "fitid", "") or "").strip()
    if not fitid:
        import hashlib
        fitid = hashlib.md5(f"{date_iso}|{amount}".encode()).hexdigest()[:12]

    payee_raw = str(getattr(txn, "name", "") or "").strip()
    memo = str(getattr(txn, "memo", "") or "").strip()

    return {
        "fitid": fitid,
        "date": date_iso,
        "amount": amount,
        "tx_type": tx_type,
        "payee_raw": payee_raw,
        "memo": memo,
        "status_override": "",
        "category_raw": "",
    }
