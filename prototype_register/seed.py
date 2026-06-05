"""Seed the prototype SQLite database with fake transactions for register testing.

Creates `prototype.db` next to this script with one account and ~10,000 transactions
spread over the last 5 years. Run once before launching `register_proto.py`.

Re-running deletes and rebuilds the database.
"""
from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "prototype.db"
NUM_TRANSACTIONS = 10_000

SCHEMA = """
CREATE TABLE account (
    id        INTEGER PRIMARY KEY,
    iri       TEXT UNIQUE NOT NULL,
    name      TEXT NOT NULL,
    type      TEXT NOT NULL,
    currency  TEXT NOT NULL DEFAULT 'GBP'
);

CREATE TABLE txn (
    id           INTEGER PRIMARY KEY,
    iri          TEXT UNIQUE NOT NULL,
    account_id   INTEGER NOT NULL REFERENCES account(id),
    posted_date  TEXT NOT NULL,
    amount       REAL NOT NULL,
    payee        TEXT,
    category     TEXT,
    status       TEXT NOT NULL CHECK(status IN ('Pending','Uncleared','Cleared','Reconciled')),
    memo         TEXT
);

CREATE INDEX idx_txn_account_date ON txn(account_id, posted_date);
CREATE INDEX idx_txn_payee        ON txn(payee);
CREATE INDEX idx_txn_status       ON txn(status);
CREATE INDEX idx_txn_category     ON txn(category);
"""

CATEGORIES_EXPENSE = [
    "Charity and gifts", "Childcare", "Dining out", "Education", "Groceries",
    "Healthcare", "Holidays and travel", "Housing", "Insurance",
    "Other expense", "Shopping", "Subscriptions", "Transport", "Utilities",
]
CATEGORIES_INCOME = ["Salary", "Investment income", "Rental income"]

PAYEES_EXPENSE = [
    "Tesco", "Sainsbury's", "Waitrose", "M&S Food", "Amazon", "Netflix",
    "Spotify", "British Gas", "Thames Water", "TfL", "Uber", "Deliveroo",
    "Pret", "John Lewis", "IKEA", "Apple", "Vodafone", "Sky", "Starbucks",
    "Boots", "Argos",
]
PAYEES_INCOME = ["Acme Corp Payroll", "HMRC Refund", "Interactive Investor"]


def seed() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO account (iri, name, type, currency) VALUES (?, ?, ?, ?)",
        ("mrl:CashAccount_1", "Current account", "cash_std", "GBP"),
    )
    account_id = conn.execute("SELECT id FROM account LIMIT 1").fetchone()[0]

    rng = random.Random(42)
    today = date.today()
    rows = []
    for i in range(NUM_TRANSACTIONS):
        is_income = rng.random() < 0.05
        offset_days = rng.randint(0, 365 * 5)
        d = (today - timedelta(days=offset_days)).isoformat()
        if is_income:
            payee = rng.choice(PAYEES_INCOME)
            category = rng.choice(CATEGORIES_INCOME)
            amount = round(rng.uniform(500, 5000), 2)
        else:
            payee = rng.choice(PAYEES_EXPENSE)
            # ~12% of expenses uncategorised to make the filter visibly useful
            category = "Uncategorised" if rng.random() < 0.12 else rng.choice(CATEGORIES_EXPENSE)
            amount = round(-rng.uniform(2, 250), 2)
        status = rng.choices(
            ["Pending", "Uncleared", "Cleared", "Reconciled"],
            weights=[1, 4, 10, 6], k=1,
        )[0]
        memo = "" if rng.random() < 0.75 else f"ref {i:05d}"
        rows.append((
            f"mfl:Transaction_{i:08x}", account_id, d, amount,
            payee, category, status, memo,
        ))

    conn.executemany(
        "INSERT INTO txn "
        "(iri, account_id, posted_date, amount, payee, category, status, memo) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    print(f"Seeded {NUM_TRANSACTIONS:,} transactions into {DB_PATH}")


if __name__ == "__main__":
    seed()
