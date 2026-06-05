"""Entry point for the desktop application.

Usage:
    python -m mfl_desktop [--db PATH] [--account-iri IRI]

If the database doesn't exist, points the user at the CLI to create it.
If no account is specified, opens the register on the first account in the DB.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from mfl_desktop.db.repository import Repository
from mfl_desktop.ui.register_window import RegisterWindow

DEFAULT_DB = Path("mfl_dev.db")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mfl_desktop")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"SQLite database path (default: {DEFAULT_DB})")
    parser.add_argument("--account-iri",
                        help="Open this account (default: first in DB)")
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(
            f"Database not found at {args.db}.\n"
            "Create one with: python -m mfl_desktop.cli init",
            file=sys.stderr,
        )
        return 1

    app = QApplication(sys.argv)
    repo = Repository(args.db)

    account_iri = args.account_iri
    if account_iri is None:
        row = repo.connection.execute(
            "SELECT iri FROM account ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            print(
                "No accounts in database.\n"
                "Initialise with: python -m mfl_desktop.cli init",
                file=sys.stderr,
            )
            return 1
        account_iri = row["iri"]

    win = RegisterWindow(repo, account_iri)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
