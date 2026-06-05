"""Command-line smoke test for the import pipeline.

Exists to exercise the lifted import engine end-to-end without a UI.
The user-facing application is PySide6 (ADR-008); this CLI is throwaway.

Usage:
    python -m mfl_desktop.cli init [--db PATH]
        Create the database (if missing) and seed a Person + one CashAccount
        if no accounts exist yet.

    python -m mfl_desktop.cli import <file> [--account-iri IRI] [--db PATH]
        [--status Cleared|Uncleared] [--accept-matches]
        Import an OFX/QFX/CSV file. Without --account-iri the first account
        in the database is used. --accept-matches merges every potential
        match with the existing manual entry; without it, all matches are
        imported as new.

    python -m mfl_desktop.cli list [--account-iri IRI] [--db PATH] [--limit N]
        List recent transactions on an account.

    python -m mfl_desktop.cli categories [--db PATH]
        Print the category tree (system + user + import-created).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mfl_desktop.account_types import ACCOUNT_TYPES
from mfl_desktop.db.repository import Repository
from mfl_desktop.import_engine.import_service import ImportService

DEFAULT_DB = Path("mfl_dev.db")


def cmd_init(args) -> int:
    repo = Repository(args.db)
    try:
        existing = repo.connection.execute(
            "SELECT id, iri, name FROM account LIMIT 1"
        ).fetchone()
        if existing is not None:
            print(
                f"Already initialised. First account: id={existing['id']} "
                f"iri={existing['iri']!r} name={existing['name']!r}"
            )
            return 0
        repo.connection.execute(
            "INSERT INTO person (iri, name, base_currency) VALUES (?, ?, ?)",
            ("mrl:Person_1", "Test User", "GBP"),
        )
        repo.connection.execute(
            "INSERT INTO account (iri, name, type, family, currency) "
            "VALUES (?, ?, ?, ?, ?)",
            ("mrl:CashAccount_1", "Current account", "cash_std", "cash", "GBP"),
        )
        repo.commit()
        print(f"Initialised {args.db} with mrl:Person_1 + mrl:CashAccount_1 (Current account, GBP)")
        return 0
    finally:
        repo.close()


def cmd_import(args) -> int:
    repo = Repository(args.db)
    try:
        account_iri = args.account_iri or _first_account_iri(repo)
        if account_iri is None:
            print("No accounts. Run `init` first.", file=sys.stderr)
            return 1

        file_bytes = Path(args.file).read_bytes()
        service = ImportService(repo)
        token, next_step = service.parse_and_stage(
            file_bytes, args.file, account_iri,
        )
        if next_step == "map":
            print(
                "File is a generic CSV needing column mapping; not supported "
                "in this CLI smoke test.",
                file=sys.stderr,
            )
            return 2

        pending = service.get_pending(token)
        assert pending is not None
        print(f"Staged {len(pending.transactions):,} transactions from {pending.filename!r}")
        print(f"  format: {pending.file_format}")
        print(
            f"  new: {pending.new_count}  "
            f"duplicates: {pending.duplicate_count}  "
            f"potential matches: {pending.match_count}"
        )
        print(
            f"  first import: {pending.is_first_import}  "
            f"suggested status: {pending.suggested_status}"
        )
        if pending.has_status_override:
            print("  source carries per-transaction status — global status not used for those rows")

        status = args.status or pending.suggested_status
        accepted: set[str] = (
            {tx.fitid for tx in pending.transactions if tx.status == "potential_match"}
            if args.accept_matches else set()
        )
        result = service.commit_import(token, status, accepted)
        print(
            f"Committed: imported={result.imported} "
            f"skipped={result.skipped} matched={result.matched} "
            f"batch_id={result.batch_id}"
        )
        return 0
    finally:
        repo.close()


def cmd_list(args) -> int:
    repo = Repository(args.db)
    try:
        account_iri = args.account_iri or _first_account_iri(repo)
        if account_iri is None:
            print("No accounts.", file=sys.stderr)
            return 1
        acct = repo.get_account_by_iri(account_iri)
        if acct is None:
            print(f"No account with iri {account_iri!r}", file=sys.stderr)
            return 1
        cur = repo.connection.execute(
            "SELECT t.posted_date, t.amount, t.status, t.memo, "
            "       COALESCE(p.name, '') AS payee, "
            "       COALESCE(c.name, '') AS category "
            "FROM txn t "
            "LEFT JOIN payee p    ON p.id = t.payee_id "
            "LEFT JOIN category c ON c.id = t.category_id "
            "WHERE t.account_id = ? "
            "ORDER BY t.posted_date DESC, t.id DESC "
            "LIMIT ?",
            (acct.id, args.limit),
        )
        for row in cur:
            amt = row["amount"] / 100
            print(
                f"  {row['posted_date']}  £{amt:>10,.2f}  "
                f"{row['status']:<11}  "
                f"{(row['payee'] or '')[:25]:<25}  "
                f"{(row['category'] or '')[:22]:<22}  "
                f"{row['memo'] or ''}"
            )
        return 0
    finally:
        repo.close()


def cmd_add_account(args) -> int:
    repo = Repository(args.db)
    try:
        acct = repo.create_account(
            name=args.name,
            type_key=args.type,
            currency=args.currency,
        )
        print(f"Created {acct.iri}: {acct.name!r} ({acct.type}, {acct.currency})")
        return 0
    finally:
        repo.close()


def cmd_categories(args) -> int:
    repo = Repository(args.db)
    try:
        cur = repo.connection.execute(
            "SELECT id, parent_id, name, source FROM category "
            "ORDER BY COALESCE(parent_id, 0), name"
        )
        children: dict[int, list[tuple[int, str, str]]] = {}
        for row in cur:
            parent = row["parent_id"] or 0
            children.setdefault(parent, []).append((row["id"], row["name"], row["source"]))
        _print_tree(children, parent_id=0, depth=0)
        return 0
    finally:
        repo.close()


def _print_tree(
    children: dict[int, list[tuple[int, str, str]]],
    parent_id: int, depth: int,
) -> None:
    for cid, name, source in children.get(parent_id, []):
        marker = "·" if source == "system" else ("+" if source == "user" else "↓")
        print(f"  {'  ' * depth}{marker} {name}  [{source}]")
        _print_tree(children, parent_id=cid, depth=depth + 1)


def _first_account_iri(repo: Repository) -> str | None:
    row = repo.connection.execute("SELECT iri FROM account LIMIT 1").fetchone()
    return row["iri"] if row else None


def main(argv: list[str] | None = None) -> int:
    # Windows console defaults to cp1252; force UTF-8 so £ and tree markers
    # render correctly. Harmless on macOS/Linux which are already UTF-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(
        prog="mfl_desktop.cli",
        description="Smoke-test CLI for the import engine.",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help="Path to SQLite database (default: mfl_dev.db)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="Create DB and seed a test account")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("import", help="Import an OFX/QFX/CSV file")
    p.add_argument("file", help="Path to the file to import")
    p.add_argument("--account-iri", help="Target account IRI (default: first account)")
    p.add_argument("--status", choices=["Cleared", "Uncleared"],
                   help="Override the suggested import status")
    p.add_argument("--accept-matches", action="store_true",
                   help="Merge all potential matches with existing manual entries")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("list", help="List recent transactions on an account")
    p.add_argument("--account-iri", help="Account IRI (default: first account)")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("categories", help="Print the category tree")
    p.set_defaults(func=cmd_categories)

    p = sub.add_parser("add-account", help="Add a new account")
    p.add_argument("name", help="Display name, e.g. 'Joint Savings'")
    p.add_argument(
        "--type", required=True,
        choices=tuple(t.key for t in ACCOUNT_TYPES),
        help="Account type",
    )
    p.add_argument(
        "--currency", default="GBP",
        help="ISO currency code (default: GBP)",
    )
    p.set_defaults(func=cmd_add_account)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
