"""Import workflow — parse, classify, stage, commit.

Mirrors the v0.1 import flow (app/core/import_engine/import_service.py) but
writes through the Repository instead of SPARQL. The classification logic
(duplicate detection by import_hash, manual-match within ±2 days, status
overrides from Banktivity) is preserved.

Key differences from v0.1:
- Instance-based (`ImportService(repo)`), not module-level globals — makes
  testing trivial and aligns with how Qt windows will hold the service.
- Categories are resolved into the hierarchical `category` table via
  Repository.find_or_create_category_path() rather than stuffed into memo.
- Status enums are plain strings ('Cleared', 'Uncleared', ...) not mflx: IRIs.
- Currency conversion to pence happens at the Repository boundary; this
  module operates in Decimal throughout.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from mfl_desktop.db.repository import Repository
from mfl_desktop.import_engine import csv_parser, ofx_parser

logger = logging.getLogger(__name__)


# ── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass
class PendingCsvMap:
    """Staged generic CSV awaiting user column mapping."""
    token: str
    account_iri: str
    filename: str
    headers: list[str]
    preview_rows: list[list[str]]
    file_bytes: bytes


@dataclass
class ClassifiedTransaction:
    fitid: str
    date_iso: str
    amount: Decimal               # always positive; direction is tx_type
    tx_type: str                  # 'debit' | 'credit'
    payee_raw: str
    memo: str
    category_raw: str             # source-supplied category path (may be empty)
    import_hash: str
    status: str                   # 'new' | 'duplicate' | 'potential_match'
    match_txn_id: Optional[int] = None
    match_txn_iri: str = ""
    match_txn_payee: str = ""
    match_txn_date: str = ""
    status_override: str = ""     # 'Cleared' | 'Reconciled' | 'Pending' | ''


@dataclass
class PendingImport:
    token: str
    account_id: int
    account_iri: str
    account_name: str
    filename: str
    file_format: str              # 'ofx' | 'qfx' | 'csv-banktivity' | 'csv-creditcard' | 'csv-generic'
    transactions: list[ClassifiedTransaction] = field(default_factory=list)
    new_count: int = 0
    duplicate_count: int = 0
    match_count: int = 0
    is_first_import: bool = True
    suggested_status: str = "Cleared"
    currency: str = "GBP"
    has_status_override: bool = False


@dataclass
class ImportResult:
    batch_id: int
    imported: int
    skipped: int
    matched: int


# ── Hash helper ─────────────────────────────────────────────────────────────


def compute_hash(account_iri: str, date_iso: str, amount: str, payee_raw: str) -> str:
    """Composite duplicate-detection hash for CSV rows without a FITID."""
    raw = f"{account_iri}|{date_iso}|{amount}|{payee_raw}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


# ── Service ─────────────────────────────────────────────────────────────────


class ImportService:
    def __init__(self, repo: Repository) -> None:
        self._repo = repo
        self._pending: dict[str, PendingImport] = {}
        self._pending_maps: dict[str, PendingCsvMap] = {}

    # ── Stage ──

    def parse_and_stage(
        self, file_bytes: bytes, filename: str, account_iri: str,
    ) -> tuple[str, str]:
        """Parse file and stage in memory.

        Returns (token, next_step):
          - next_step == 'preview' — go to preview using token.
          - next_step == 'map'     — the file is a generic CSV needing column
                                     mapping; pass token to apply_mapping_and_stage().
        """
        ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

        if ext in ("ofx", "qfx"):
            raw_txns = ofx_parser.parse_ofx(file_bytes, filename)
            has_override = False
            file_format = "qfx" if ext == "qfx" else "ofx"
        elif ext == "csv":
            content = csv_parser._decode(file_bytes)
            fmt = csv_parser._detect_format(content.splitlines())
            if fmt == "generic":
                token = self._stage_for_mapping(file_bytes, filename, account_iri)
                return token, "map"
            raw_txns, has_override, _label = csv_parser.parse_csv(file_bytes, filename)
            file_format = f"csv-{fmt}"
        else:
            raise ValueError(
                f"Unsupported file format '.{ext}'. "
                "Please upload an OFX, QFX, or CSV file."
            )

        token = self._classify_and_stage(
            raw_txns, has_override, file_format, account_iri, filename,
        )
        return token, "preview"

    def _stage_for_mapping(
        self, file_bytes: bytes, filename: str, account_iri: str,
    ) -> str:
        content = csv_parser._decode(file_bytes)
        reader = csv.DictReader(io.StringIO(content))
        headers = list(reader.fieldnames or [])
        preview_rows: list[list[str]] = []
        for i, row in enumerate(reader):
            if i >= 5:
                break
            preview_rows.append([str(row.get(h, "")) for h in headers])
        token = uuid.uuid4().hex[:16]
        self._pending_maps[token] = PendingCsvMap(
            token=token, account_iri=account_iri, filename=filename,
            headers=headers, preview_rows=preview_rows, file_bytes=file_bytes,
        )
        return token

    def _classify_and_stage(
        self,
        raw_txns: list[dict],
        has_override: bool,
        file_format: str,
        account_iri: str,
        filename: str,
    ) -> str:
        acct = self._repo.get_account_by_iri(account_iri)
        if acct is None:
            raise ValueError(f"No account with IRI {account_iri!r}")

        first = not self._repo.account_has_transactions(acct.id)
        suggested = "Cleared" if first else "Uncleared"

        classified: list[ClassifiedTransaction] = []
        new_count = dup_count = match_count = 0
        # Hashes already assigned to a row in *this* batch. Used to resolve
        # within-batch collisions on the composite-hash path — two CSV rows
        # with the same date, amount, and payee (very common when the source
        # has no payee column, or when the user does two coffees on the same
        # day at the same price) would otherwise produce the same hash and
        # blow up the UNIQUE(account_id, import_hash) constraint on commit.
        # The suffix is deterministic, so re-importing the same file gets the
        # same hashes and cross-batch duplicate detection still works.
        batch_seen_hashes: set[str] = set()

        for raw in raw_txns:
            date_iso = raw["date"]
            amount = raw["amount"]
            tx_type = raw["tx_type"]
            payee_raw = raw.get("payee_raw", "")
            memo = raw.get("memo", "")
            category_raw = raw.get("category_raw", "")
            status_ov = raw.get("status_override", "")

            fitid = raw.get("fitid", "")
            if fitid:
                import_hash = fitid
            else:
                base_hash = compute_hash(account_iri, date_iso, str(amount), payee_raw)
                import_hash = base_hash
                n = 1
                while import_hash in batch_seen_hashes:
                    import_hash = f"{base_hash}:{n}"
                    n += 1
                fitid = import_hash
            batch_seen_hashes.add(import_hash)

            # Signed amount in the database carries direction; the matcher
            # compares against that, so we sign here.
            signed_amount = -amount if tx_type == "debit" else amount

            if self._repo.import_hash_exists(acct.id, import_hash):
                status = "duplicate"
                dup_count += 1
                match_id = None
                match_iri = match_payee = match_date = ""
            else:
                manual = self._repo.find_manual_match(
                    acct.id, date_iso, signed_amount,
                )
                if manual is not None:
                    status = "potential_match"
                    match_id = manual.id
                    match_iri = manual.iri
                    match_payee = manual.payee_raw
                    match_date = manual.posted_date
                    match_count += 1
                else:
                    status = "new"
                    new_count += 1
                    match_id = None
                    match_iri = match_payee = match_date = ""

            classified.append(ClassifiedTransaction(
                fitid=fitid, date_iso=date_iso, amount=amount,
                tx_type=tx_type, payee_raw=payee_raw, memo=memo,
                category_raw=category_raw, import_hash=import_hash,
                status=status, match_txn_id=match_id, match_txn_iri=match_iri,
                match_txn_payee=match_payee, match_txn_date=match_date,
                status_override=status_ov,
            ))

        token = uuid.uuid4().hex[:16]
        self._pending[token] = PendingImport(
            token=token, account_id=acct.id, account_iri=account_iri,
            account_name=acct.name, filename=filename,
            file_format=file_format, transactions=classified,
            new_count=new_count, duplicate_count=dup_count,
            match_count=match_count, is_first_import=first,
            suggested_status=suggested, currency=acct.currency,
            has_status_override=has_override,
        )
        return token

    def get_pending(self, token: str) -> Optional[PendingImport]:
        return self._pending.get(token)

    def get_pending_map(self, token: str) -> Optional[PendingCsvMap]:
        return self._pending_maps.get(token)

    def discard_pending_map(self, token: str) -> None:
        """Drop a staged mapping session (e.g. user cancelled the wizard).

        Idempotent — no error if the token is already gone. Frees the staged
        file bytes the PendingCsvMap was holding in memory.
        """
        self._pending_maps.pop(token, None)

    def apply_mapping_and_stage(
        self, token: str, mapping: csv_parser.CsvColumnMapping,
    ) -> str:
        pending_map = self._pending_maps.get(token)
        if pending_map is None:
            raise ValueError("Mapping session not found or expired.")
        content = csv_parser._decode(pending_map.file_bytes)
        raw_txns = csv_parser.parse_with_mapping(content, mapping)
        del self._pending_maps[token]
        return self._classify_and_stage(
            raw_txns, has_override=False, file_format="csv-generic",
            account_iri=pending_map.account_iri,
            filename=pending_map.filename,
        )

    # ── Commit ──

    def commit_import(
        self,
        token: str,
        import_status: str,                # 'Cleared' | 'Uncleared'
        accepted_match_fitids: set[str],
    ) -> ImportResult:
        pending = self._pending.get(token)
        if pending is None:
            raise ValueError("Import session not found or expired.")
        if import_status not in ("Cleared", "Uncleared"):
            raise ValueError(f"Unknown import status: {import_status!r}")

        try:
            batch_id = self._repo.create_import_batch(
                account_id=pending.account_id,
                source_format=pending.file_format,
                source_filename=pending.filename,
            )
            imported = skipped = matched = 0

            for tx in pending.transactions:
                if tx.status == "duplicate":
                    skipped += 1
                    continue

                if tx.status == "potential_match" and tx.fitid in accepted_match_fitids:
                    # match_txn_id is always set when status == 'potential_match'
                    assert tx.match_txn_id is not None
                    self._repo.merge_into_manual_transaction(
                        manual_id=tx.match_txn_id,
                        import_hash=tx.import_hash,
                        memo=tx.memo,
                    )
                    matched += 1
                    continue

                effective_status = tx.status_override or import_status
                category_id = self._resolve_category_id(tx.category_raw)
                payee_id = self._repo.get_or_create_payee(tx.payee_raw)
                signed_amount = -tx.amount if tx.tx_type == "debit" else tx.amount

                self._repo.insert_transaction(
                    account_id=pending.account_id,
                    posted_date=tx.date_iso,
                    amount=signed_amount,
                    payee_id=payee_id,
                    category_id=category_id,
                    status=effective_status,
                    memo=tx.memo,
                    import_hash=tx.import_hash,
                    import_batch_id=batch_id,
                )
                imported += 1

            self._repo.finalise_import_batch(
                batch_id=batch_id,
                new_count=imported,
                duplicate_count=skipped,
                matched_count=matched,
            )
            self._repo.commit()
        except Exception:
            self._repo.rollback()
            raise

        del self._pending[token]
        logger.info(
            f"Import committed: {imported} new, {skipped} skipped, "
            f"{matched} matched (batch id {batch_id})"
        )
        return ImportResult(
            batch_id=batch_id, imported=imported,
            skipped=skipped, matched=matched,
        )

    def _resolve_category_id(self, category_raw: str) -> int:
        """Parse a source category path (Banktivity ':' separator) into the
        hierarchical category tree. Returns the leaf id, creating intermediate
        nodes as needed; returns Uncategorised if path is empty.
        """
        if not category_raw or not category_raw.strip():
            return self._repo.uncategorised_id()
        segments = [s.strip() for s in category_raw.split(":") if s.strip()]
        return self._repo.find_or_create_category_path(segments, source="import")
