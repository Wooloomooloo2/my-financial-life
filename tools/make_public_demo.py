#!/usr/bin/env python3
"""Generate a realistic, fictional 12-month demo file for marketing/website/
store screenshots — so we never screenshot the owner's real data.

Builds ``mfl_public.mfl`` from scratch for a made-up UK persona ("Jordan
Avery", GBP base) with a full financial picture: current + savings accounts,
a rewards credit card paid off monthly, a Stocks & Shares ISA and a USD
brokerage (multi-currency) and a workplace pension all with price history and
dividends, a home and a car, twelve months of categorised income and spending
with believable payees, regular transfers, and a populated budget.

Deterministic (fixed RNG seed) so re-running produces the same file. Safe to
re-run: it removes any existing mfl_public.mfl first. Run from the repo root:

    python tools/make_public_demo.py
"""
from __future__ import annotations

import calendar
import random
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mfl_desktop.db.repository import Repository  # noqa: E402

OUT = Path("mfl_public.mfl")
TODAY = date(2026, 6, 21)            # fixed "now" so the file is reproducible
RNG = random.Random(20260621)

# 12 whole months ending in the current month: 2025-07 .. 2026-06
MONTHS: list[tuple[int, int]] = []
y, m = 2025, 7
for _ in range(12):
    MONTHS.append((y, m))
    m += 1
    if m > 12:
        m = 1
        y += 1


def D(x) -> Decimal:
    return Decimal(str(x))


def day(yr: int, mo: int, dom: int) -> str:
    """A safe ISO date string clamped to the month's length, never in the
    future relative to TODAY (so 'this month' looks mid-stream, not complete)."""
    dom = min(dom, calendar.monthrange(yr, mo)[1])
    d = date(yr, mo, dom)
    if d > TODAY:
        d = TODAY
    return d.isoformat()


def jitter(base: float, pct: float = 0.12) -> Decimal:
    """A naturally-varying amount around ``base`` (±pct), 2dp."""
    f = base * (1 + RNG.uniform(-pct, pct))
    return D(round(f, 2))


def status_for(iso: str) -> str:
    age = (TODAY - date.fromisoformat(iso)).days
    if age > 45:
        return "Reconciled"
    if age <= 4:
        return "Pending"
    return "Cleared"


def main() -> int:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(OUT) + suffix)
        if p.exists():
            p.unlink()

    repo = Repository(OUT)
    conn = repo.connection

    # Person + base currency ------------------------------------------------
    conn.execute(
        "INSERT INTO person (iri, name, base_currency) VALUES (?, ?, ?)",
        ("mrl:Person_1", "Jordan Avery", "GBP"),
    )
    repo.commit()
    repo.set_setting("base_currency", "GBP")

    # Accounts --------------------------------------------------------------
    current = repo.create_account(
        name="Everyday Current", type_key="cash", currency="GBP",
        opening_balance=D("7000.00"),
    )
    emergency = repo.create_account(
        name="Emergency Fund", type_key="savings", currency="GBP",
        opening_balance=D("15000.00"),
    )
    holiday = repo.create_account(
        name="Holiday Pot", type_key="savings", currency="GBP",
        opening_balance=D("1200.00"),
    )
    card = repo.create_account(
        name="Aspire Rewards Card", type_key="credit", currency="GBP",
        opening_balance=D("0.00"), credit_limit=D("6000.00"),
    )
    isa = repo.create_account(
        name="Stocks & Shares ISA", type_key="investment", currency="GBP",
        opening_balance=D("0.00"),
    )
    brokerage = repo.create_account(
        name="US Brokerage", type_key="investment", currency="USD",
        opening_balance=D("0.00"),
    )
    pension = repo.create_account(
        name="Workplace Pension", type_key="investment", currency="GBP",
        opening_balance=D("0.00"),
    )
    repo.create_account(
        name="Home", type_key="property", currency="GBP",
        opening_balance=D("315000.00"),
    )
    repo.create_account(
        name="Car", type_key="vehicle", currency="GBP",
        opening_balance=D("15500.00"),
    )

    transfer_cat = repo.get_default_transfer_category_id()

    # Categories: use the seeded tree, add a few subcategories for richer
    # reports. cat() resolves any (sub)category by name.
    def top(name: str) -> int:
        row = conn.execute(
            "SELECT id FROM category WHERE name = ? AND parent_id IN "
            "(SELECT id FROM category WHERE name IN "
            "('Income','Expense') OR parent_id IS NULL)", (name,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id FROM category WHERE name = ?", (name,)
            ).fetchone()
        return int(row["id"])

    subs: dict[str, int] = {}

    def add_subs(parent_name: str, names: list[str]) -> None:
        parent_id = top(parent_name)
        for n in names:
            subs[n] = repo.create_category(n, parent_id, "expense")

    add_subs("Housing", ["Rent / Mortgage", "Council Tax", "Home insurance"])
    add_subs("Utilities", ["Energy", "Water", "Broadband & Mobile"])
    add_subs("Transport", ["Fuel", "Public transport", "Car insurance"])
    add_subs("Dining out", ["Restaurants", "Coffee", "Takeaway"])
    add_subs("Subscriptions", ["Streaming", "Music", "Gym"])

    def cat(name: str) -> int:
        return subs.get(name) or top(name)

    # Loan accounts (ADR-095), created now that categories exist (the mortgage
    # books its interest under Rent / Mortgage so the budget housing line still
    # captures the true cost). Opening balance = current principal owed
    # (original − already paid); monthly payments (posted in the loop below)
    # then build the register history and pay it down.
    mortgage_id = repo.create_loan_account(
        name="Home Mortgage", currency="GBP",
        original_amount=D("230000.00"), principal_paid=D("23000.00"),  # owed 207k
        interest_rate=4.49, compounding="monthly", term_months=300,    # 25 yr
        payment=D("1150.00"),                                          # = old rent
        start_date=day(2025, 7, 1), payment_day=1,
        track_mode="split", interest_source="payment",
        payment_account_id=current.id,
        interest_category_id=cat("Rent / Mortgage"),
    )
    car_loan_id = repo.create_loan_account(
        name="Car Finance", currency="GBP",
        original_amount=D("14000.00"), principal_paid=D("5200.00"),    # owed 8.8k
        interest_rate=6.9, compounding="monthly", term_months=60,
        start_date=day(2025, 7, 15), payment_day=15,
        track_mode="split", interest_source="payment",
        payment_account_id=current.id,
        # interest_category left default → 'Interest ▸ Loan interest'
    )

    # Payees are created on demand.
    def pay(name: str) -> int:
        return repo.get_or_create_payee(name)

    # ── Cash transactions ──────────────────────────────────────────────────
    def spend(acct, iso, amount, category, payee, memo=""):
        repo.insert_transaction(
            account_id=acct.id, posted_date=iso, amount=-abs(D(amount)),
            payee_id=pay(payee), category_id=cat(category),
            status=status_for(iso), memo=memo,
            import_hash=None, import_batch_id=None,
        )

    def earn(acct, iso, amount, category, payee, memo=""):
        repo.insert_transaction(
            account_id=acct.id, posted_date=iso, amount=abs(D(amount)),
            payee_id=pay(payee), category_id=cat(category),
            status=status_for(iso), memo=memo,
            import_hash=None, import_batch_id=None,
        )

    def move(src, dst, iso, amount, to_amount=None):
        repo.create_transfer(
            from_account_id=src.id, to_account_id=dst.id, posted_date=iso,
            amount=D(amount), category_id=transfer_cat,
            status=status_for(iso),
            to_amount=D(to_amount) if to_amount is not None else None,
        )

    GROCERS = ["Tesco", "Sainsbury's", "Aldi", "M&S Food", "Waitrose"]
    COFFEE = ["Pret a Manger", "Costa Coffee", "Caffè Nero"]
    RESTAURANTS = ["Nando's", "Wagamama", "The Ivy", "Pizza Express", "Dishoom"]
    TAKEAWAY = ["Deliveroo", "Just Eat", "Uber Eats"]
    FUEL = ["Shell", "BP", "Esso"]
    SHOPS = ["Amazon", "John Lewis", "Zara", "IKEA", "Uniqlo", "Apple Store"]

    # ── Per-month recurring + discretionary ─────────────────────────────────
    for (yr, mo) in MONTHS:
        # Income — salary
        earn(current, day(yr, mo, 25), jitter(4600, 0.01), "Salary",
             "Northwind Software Ltd", "Monthly salary")
        # Savings interest (small)
        earn(emergency, day(yr, mo, 28), jitter(34, 0.2), "Investment income",
             "Interest", "Interest")

        # Mortgage + car-loan payments (ADR-095) — each splits into principal
        # (a transfer that pays the loan down) + interest (an expense on the
        # paying account), building the loans' register history. The mortgage
        # interest lands under Rent / Mortgage so the budget housing line still
        # shows the true cost.
        repo.post_loan_payment(
            account_id=mortgage_id, posted_date=day(yr, mo, 1),
            status=status_for(day(yr, mo, 1)),
        )
        repo.post_loan_payment(
            account_id=car_loan_id, posted_date=day(yr, mo, 15),
            status=status_for(day(yr, mo, 15)),
        )

        # Other housing & bills (current account direct debits)
        spend(current, day(yr, mo, 5), 168, "Council Tax", "City Council")
        # Energy varies by season (higher in winter)
        season = 1.5 if mo in (12, 1, 2) else (0.7 if mo in (6, 7, 8) else 1.0)
        spend(current, day(yr, mo, 8), jitter(95 * season, 0.1), "Energy",
              "Octopus Energy")
        spend(current, day(yr, mo, 8), jitter(41, 0.05), "Water",
              "Thames Water")
        spend(current, day(yr, mo, 10), 62, "Broadband & Mobile", "BT")
        spend(current, day(yr, mo, 10), 22, "Broadband & Mobile", "Vodafone")
        spend(current, day(yr, mo, 12), 15.99, "Streaming", "Netflix")
        spend(current, day(yr, mo, 12), 17.99, "Streaming", "Disney+")
        spend(current, day(yr, mo, 12), 10.99, "Music", "Spotify")
        spend(current, day(yr, mo, 3), 42, "Gym", "PureGym")
        spend(current, day(yr, mo, 15), 58, "Car insurance", "Aviva")
        spend(current, day(yr, mo, 15), 21, "Home insurance", "Aviva")
        spend(current, day(yr, mo, 6), 20, "Charity and gifts",
              "Oxfam", "Monthly donation")

        # Groceries — 4–5 trips, mostly on the credit card
        for _ in range(RNG.randint(4, 5)):
            dom = RNG.randint(2, 27)
            spend(card, day(yr, mo, dom), jitter(78, 0.35), "Groceries",
                  RNG.choice(GROCERS))
        # Dining / coffee / takeaway on the card
        for _ in range(RNG.randint(2, 4)):
            spend(card, day(yr, mo, RNG.randint(2, 27)), jitter(34, 0.4),
                  "Restaurants", RNG.choice(RESTAURANTS))
        for _ in range(RNG.randint(3, 6)):
            spend(card, day(yr, mo, RNG.randint(2, 27)), jitter(4.2, 0.3),
                  "Coffee", RNG.choice(COFFEE))
        for _ in range(RNG.randint(1, 3)):
            spend(card, day(yr, mo, RNG.randint(2, 27)), jitter(26, 0.4),
                  "Takeaway", RNG.choice(TAKEAWAY))
        # Fuel + public transport
        for _ in range(RNG.randint(1, 2)):
            spend(card, day(yr, mo, RNG.randint(2, 27)), jitter(56, 0.2),
                  "Fuel", RNG.choice(FUEL))
        spend(card, day(yr, mo, RNG.randint(2, 27)), jitter(48, 0.3),
              "Public transport", "Trainline")
        # Shopping — a couple of items
        for _ in range(RNG.randint(1, 3)):
            spend(card, day(yr, mo, RNG.randint(2, 27)), jitter(45, 0.6),
                  "Shopping", RNG.choice(SHOPS))
        # Healthcare occasionally
        if RNG.random() < 0.5:
            spend(card, day(yr, mo, RNG.randint(2, 27)), jitter(18, 0.5),
                  "Healthcare", "Boots Pharmacy")

        # Regular savings + investing transfers (just after payday)
        move(current, emergency, day(yr, mo, 26), 300)
        move(current, holiday, day(yr, mo, 26), 75)
        move(current, isa, day(yr, mo, 26), 300)
        move(current, pension, day(yr, mo, 25), 250)

    # Seasonal one-offs ------------------------------------------------------
    spend(card, day(2025, 8, 14), 720, "Holidays and travel",
          "British Airways", "Summer flights")
    spend(card, day(2025, 8, 16), 980, "Holidays and travel",
          "Booking.com", "Summer hotel")
    spend(card, day(2025, 11, 21), 899, "Shopping", "Apple Store",
          "New iPhone")
    spend(card, day(2025, 12, 18), 540, "Charity and gifts", "Amazon",
          "Christmas gifts")
    spend(card, day(2026, 2, 13), 260, "Restaurants", "The Ivy",
          "Anniversary dinner")
    spend(current, day(2026, 4, 9), 1450, "Housing", "Handy Builders",
          "Bathroom repair")

    # Pay the card off monthly (final pass, now that ALL charges — recurring +
    # seasonal — exist): on the 18th clear everything owed up to the 17th.
    # Charges dated later in the month roll to the next payment, and the latest
    # (current) month is left carrying a small live balance — realistic.
    for (yr, mo) in MONTHS:
        owed = -repo.balance_as_of(card.id, day(yr, mo, 17))   # negative = owed
        if owed > 0:
            move(current, card, day(yr, mo, 18), owed)

    # ── Investments ─────────────────────────────────────────────────────────
    vwrl = repo.get_or_create_security(
        "Vanguard FTSE All-World UCITS ETF", "VWRL", "ETF")
    vusa = repo.get_or_create_security(
        "Vanguard S&P 500 UCITS ETF", "VUSA", "ETF")
    aapl = repo.get_or_create_security("Apple Inc", "AAPL", "Stock")
    msft = repo.get_or_create_security("Microsoft Corp", "MSFT", "Stock")
    glbl = repo.get_or_create_security(
        "L&G Global Equity Index Fund", "", "Fund")

    def buy(acct, iso, sec, qty, price, ccy=None):
        amt = (D(qty) * D(price)).quantize(D("0.01"))
        repo.insert_transaction(
            account_id=acct.id, posted_date=iso, amount=-amt,
            payee_id=None, category_id=top("Savings and investments"),
            status=status_for(iso), memo="", action="Buy",
            security_id=sec, quantity=D(qty), price=D(price),
            import_hash=None, import_batch_id=None,
        )

    def shares_in(acct, iso, sec, qty, price):
        # Seed a pre-existing holding (zero cash, carries cost basis at price).
        repo.insert_transaction(
            account_id=acct.id, posted_date=iso, amount=D("0.00"),
            payee_id=None, category_id=top("Savings and investments"),
            status="Reconciled", memo="Opening holding", action="ShrsIn",
            security_id=sec, quantity=D(qty), price=D(price),
            import_hash=None, import_batch_id=None,
        )

    def dividend(acct, iso, sec, amount):
        repo.insert_transaction(
            account_id=acct.id, posted_date=iso, amount=abs(D(amount)),
            payee_id=pay("Dividend"), category_id=top("Investment income"),
            status=status_for(iso), memo="Dividend", action="Div",
            security_id=sec, quantity=None, price=None,
            import_hash=None, import_batch_id=None,
        )

    start = day(2025, 7, 1)
    # Pre-existing holdings (the picture didn't start from zero)
    shares_in(isa, start, vwrl, 230, 96.50)
    shares_in(isa, start, vusa, 60, 78.20)
    shares_in(pension, start, glbl, 405, 178.00)   # ~£72k starting pot

    # Fund + stock up the USD brokerage at the start (cross-currency)
    move(current, brokerage, start, 6000, to_amount=7680)   # ~1.28 GBP→USD
    buy(brokerage, day(2025, 7, 2), aapl, 20, 191.00)
    buy(brokerage, day(2025, 7, 2), msft, 8, 441.00)

    # Monthly ISA + pension buying with the transferred cash; quarterly divs
    vwrl_px = 96.5
    glbl_px = 178.0
    for i, (yr, mo) in enumerate(MONTHS):
        vwrl_px = round(vwrl_px * (1 + RNG.uniform(0.002, 0.018)), 2)
        glbl_px = round(glbl_px * (1 + RNG.uniform(0.001, 0.016)), 2)
        # ISA: the monthly £300 contribution buys VWRL
        buy(isa, day(yr, mo, 27), vwrl, round(300 / vwrl_px, 4), vwrl_px)
        # Pension: monthly £250 contribution buys the global fund
        buy(pension, day(yr, mo, 26), glbl, round(250 / glbl_px, 4), glbl_px)
        # Quarterly dividends
        if mo in (3, 6, 9, 12):
            dividend(isa, day(yr, mo, 20), vwrl, jitter(190, 0.1))
            dividend(brokerage, day(yr, mo, 15), aapl, jitter(8, 0.1))
            dividend(brokerage, day(yr, mo, 15), msft, jitter(9, 0.1))

    # A mid-year brokerage top-up
    move(current, brokerage, day(2026, 1, 12), 1500, to_amount=1905)
    buy(brokerage, day(2026, 1, 13), aapl, 4, 205.00)
    buy(brokerage, day(2026, 1, 13), msft, 2, 462.00)

    # ── A bond + an option in the brokerage (ADR-093) ───────────────────────
    aapl_bond = repo.get_or_create_security(
        "Apple Inc 4.2% 2032", "", "Bond",
        instrument_type="bond", price_multiplier=10.0, face_value=1000.0,
        coupon_rate=4.2, maturity_date=day(2032, 9, 1), cusip="037833ET3",
    )
    aapl_call = repo.get_or_create_security(
        "AAPL 18-Jun-2027 220 Call", "", "Option",
        instrument_type="option", price_multiplier=100.0, contract_size=100.0,
        underlying_symbol="AAPL", strike=220.0, expiry_date=day(2027, 6, 18),
        option_type="call",
    )
    # Fund the positions, then buy: 5 bonds @ 99.50 (% of par) + £accrued, and
    # 2 call contracts @ 3.40 (premium/share, ×100 multiplier).
    move(current, brokerage, day(2025, 8, 1), 4600, to_amount=5870)
    bond_principal = (D("5") * D("1000") * D("99.50") / D("100"))  # 4975.00
    bond_accrued = D("58.30")
    repo.insert_transaction(
        account_id=brokerage.id, posted_date=day(2025, 8, 4),
        amount=-(bond_principal + bond_accrued), payee_id=None,
        category_id=top("Savings and investments"), status="Cleared", memo="",
        action="Buy", security_id=aapl_bond, quantity=D("5"), price=D("99.50"),
        accrued_interest=bond_accrued, import_hash=None, import_batch_id=None,
    )
    repo.insert_transaction(
        account_id=brokerage.id, posted_date=day(2025, 8, 4),
        amount=-(D("2") * D("3.40") * D("100")), payee_id=None,
        category_id=top("Savings and investments"), status="Cleared", memo="",
        action="Buy", security_id=aapl_call, quantity=D("2"), price=D("3.40"),
        import_hash=None, import_batch_id=None,
    )
    repo.commit()

    # ── Price history (monthly close per security) + FX ─────────────────────
    def price_series(sec, p0, p1, ccy):
        for i, (yr, mo) in enumerate(MONTHS):
            frac = i / (len(MONTHS) - 1)
            px = p0 + (p1 - p0) * frac
            px = round(px * (1 + RNG.uniform(-0.02, 0.02)), 2)
            repo.upsert_security_price(
                security_id=sec, price_date=day(yr, mo, 28),
                price=px, source="manual", currency=ccy,
            )

    price_series(vwrl, 96.5, 114.0, "GBP")
    price_series(vusa, 78.2, 92.5, "GBP")
    price_series(glbl, 178.0, 208.0, "GBP")
    price_series(aapl, 191.0, 219.0, "USD")
    price_series(msft, 441.0, 489.0, "USD")
    # Bond quotes as a % of par; option as a premium/share. A couple of recent
    # marks so they carry a market value (×price_multiplier) in Net Worth.
    price_series(aapl_bond, 99.5, 101.1, "USD")
    price_series(aapl_call, 3.4, 5.2, "USD")

    # FX: monthly USD→GBP (so the brokerage values in the GBP net worth)
    usdgbp = 0.781
    for (yr, mo) in MONTHS:
        usdgbp = round(usdgbp * (1 + RNG.uniform(-0.01, 0.01)), 4)
        repo.upsert_fx_rate(
            date=day(yr, mo, 28), base="USD", quote="GBP",
            rate=D(usdgbp), source="manual",
        )

    # ── Budget (Jan–Dec 2026), populated so the Budget screen shows well ─────
    budget = repo.create_budget(
        name="2026 Household Budget", start_month="2026-01",
        length_months=12, currency="GBP",
    )
    repo.set_budget_accounts(budget.id, [
        (current.id, "balance"), (card.id, "available_credit"),
    ])
    plan = {
        "Salary": 3950, "Rent / Mortgage": 800, "Groceries": 360,
        "Energy": 110, "Water": 41, "Broadband & Mobile": 84,
        "Council Tax": 168, "Restaurants": 120, "Coffee": 30,
        "Takeaway": 45, "Fuel": 95, "Public transport": 50,
        "Streaming": 34, "Music": 11, "Gym": 42, "Car insurance": 58,
        "Home insurance": 21, "Shopping": 140, "Healthcare": 25,
        "Holidays and travel": 200, "Charity and gifts": 40,
    }
    for name, amt in plan.items():
        cid = cat(name)
        kind = "income" if name == "Salary" else "expense"
        line_id = repo.add_budget_line(budget_id=budget.id, category_id=cid)
        repo.set_line_allocation(line_id, "2026-01", D(amt), scope="all")

    # Track the mortgage pay-off in the budget (ADR-095 → ADR-058 R4b goal).
    repo.create_loan_paydown_goal(mortgage_id, budget.id)

    # Pull a couple of bills in (ADR-094) so the stepped burn-down shows
    # scheduled bills — these link the existing envelopes to a schedule.
    council_sched = repo.create_scheduled_txn(
        account_id=current.id, payee_name="City Council",
        category_id=cat("Council Tax"), estimated_amount=D("-168.00"),
        cadence="monthly", anchor_date="2026-01-05",
    )
    repo.add_bill_line_from_schedule(budget_id=budget.id, schedule_id=council_sched)
    gym_sched = repo.create_scheduled_txn(
        account_id=current.id, payee_name="PureGym",
        category_id=cat("Gym"), estimated_amount=D("-42.00"),
        cadence="monthly", anchor_date="2026-01-05",
    )
    repo.add_bill_line_from_schedule(budget_id=budget.id, schedule_id=gym_sched)

    # ── Summary ─────────────────────────────────────────────────────────────
    repo.checkpoint()
    values = repo.compute_account_values()
    nw = Decimal("0")
    print(f"\nCreated {OUT} for Jordan Avery (GBP):\n")
    for a in repo.list_accounts():
        native = values.get(a.id, Decimal("0"))
        conv, _ = repo.convert_amount(
            native, from_ccy=a.currency, to_ccy="GBP",
            on_date=TODAY.isoformat(),
        )
        if conv is not None:
            nw += conv
        tag = "" if a.currency == "GBP" else f" ({a.currency} {native:,.2f})"
        print(f"  {a.name:<22} {a.currency} "
              f"{(conv if conv is not None else native):>12,.2f} GBP{tag}")
    ntxn = conn.execute("SELECT COUNT(*) c FROM txn").fetchone()["c"]
    print(f"\n  Net worth: £{nw:,.2f}   |   {ntxn} transactions   |   "
          f"{len(MONTHS)} months")
    repo.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
