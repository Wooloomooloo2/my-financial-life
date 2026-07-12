#!/usr/bin/env python3
"""Standard Life pension statement (PDF) → investment QIF + unit-price history.

ADR-153. Operator tool; not shipped with the app.

Standard Life will not export anything machine-readable for a Group Stakeholder
plan — the transaction statement is a PDF. This turns that PDF into a QIF the
app's investment importer already understands, and recovers a unit-price history
the statement never actually prints.

    python tools/standard_life_pdf_to_qif.py statement.pdf --account "Standard Life Pension"

Writes ``<account>.qif`` and ``<account>-prices.csv`` next to the PDF. The QIF
imports through the normal Import flow; the price CSV has no importer (the app
has no price-import path) and is loaded with ``--load-prices DB`` or typed into
the Stock Record dialog.

The tool REFUSES TO WRITE (exit 1) unless the replayed closing position matches
the statement's own investment summary to the unit. See "Reconciliation" below —
that check is the whole reason this is trustworthy, so don't remove it.

--- What the statement gives us -------------------------------------------------

Three things: the contributions ("How payments were invested"), the automatic
Lifestyle phased switches, and the closing position ("Investment summary").

A switch is not a partial rebalance — it liquidates the WHOLE plan and rebuys it
in the new target mix, so "Amount of switch" is the entire plan value that day.
That makes each switch two simultaneous equations in the two unit prices:

    sold_a·Pa   + sold_b·Pb   = total      (what was sold is the whole plan)
    bought_a·Pa + bought_b·Pb = total      (…and it all went straight back in)

which solves for both prices on every switch date. This is the only source of
unit prices in the document: the statement prints just one price per fund, on
the closing date. Sanity check on the owner's 2026 statement — the solved prices
for the closing date come out at 123.91p / 116.38p, and the statement
independently publishes 124.0p / 116.4p. Nothing was fed in.

--- Two things that need care ---------------------------------------------------

**Policy credits.** Units drift upward between switches: a switch sells slightly
more units than the previous one bought. That drift is Standard Life crediting
free units (the statement's "Total credits" line — £513.08 on the 2026 one). We
inject them as ``ShrsIn`` (share-in, zero cash), which is what a credit is, so
the replay lands on the statement's closing units exactly.

Their *dates* are not recoverable. The 2026 statement itemises nothing between
2005 and 2023, yet units grew 2,012 → 2,203 across that window, so ~190 units
get booked in one lump at the first switch that reveals them. Unit counts stay
exact; **cost basis on this account is therefore approximate** and any gain/loss
figure is indicative. In a tax-free wrapper that's an acceptable trade.

**Share-class rebrands.** Occasionally a "switch" sells every old fund and buys
funds with entirely new names (2025-07-29: ``SL Managed P`` → ``SLMixAstMgdS7P``).
That is a re-designation of the same units, not a trade. Recorded as
``ShrsOut``/``ShrsIn``, which carries cost basis across (ADR-053) rather than
booking a phantom realised gain — on the owner's plan a Sell+Buy would have
invented an ~£8,000 gain. It is also the one switch whose prices are NOT
recoverable: four unknowns, two equations. It contributes no prices, and needs
none, because no cash moves.

--- Cash ------------------------------------------------------------------------

A pension has no cash sleeve; a contribution arrives and is invested same-day.
By default we emit only the ``Buy`` leg, on the assumption the contribution is
already in the account as a cash row (it was for the owner — 7 rows from an
earlier CSV import, ADR-148 batch 43). Cash therefore lands at exactly zero.

Pass ``--contrib-cash`` for an empty account: it emits a ``Contrib`` row before
each ``Buy`` so the account funds itself. Passing it when the cash rows already
exist DOUBLES the money — the reconciliation check will not catch this, because
it checks units, not cash. It is the one foot-gun here.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

try:
    from pypdf import PdfReader
except ImportError:                                          # pragma: no cover
    sys.exit("pypdf is required:  pip install pypdf")

TOL = Decimal("0.0005")            # unit-count tolerance; statement prints 3dp
CREDIT_MEMO = "Policy credit (units added by Standard Life)"

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}
SHORT = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul",
     "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}

# Fund names: "SLMixAstMgdS7P" (new style) or "SL Managed P" (old, spaced).
FUND_RE = re.compile(r"(SL[A-Za-z0-9]+|SL [A-Za-z0-9 ]*?P)(?=\s{2,}|$)")
QTY_RE = re.compile(r"(sold|bought) ([\d.]+) units")


def money(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"))


def _num(s: str) -> Decimal:
    return Decimal(s.replace(",", "").replace("£", ""))


# ── Parsing ──────────────────────────────────────────────────────────────────


def read_pdf(path: Path) -> str:
    """Layout mode is mandatory: the switch tables are two side-by-side columns
    and plain extraction interleaves them into nonsense."""
    reader = PdfReader(str(path))
    return "\n".join(p.extract_text(extraction_mode="layout") for p in reader.pages)


def parse_closing(text: str) -> dict[str, tuple[Decimal, Decimal]]:
    """Investment summary → {fund: (units, value)}.

    We deliberately take the fund's *value*, not its printed price: the printed
    price is rounded to 0.1p and does not reproduce the stated value. The exact
    price is value / units.

        SLMixAstMgdS7P        124.0p     2,992.432     1.00%£3707.86
    """
    out: dict[str, tuple[Decimal, Decimal]] = {}
    pat = re.compile(
        r"^\s*(SL[A-Za-z0-9]+)\s+[\d.]+p\s+([\d,.]+)\s+[\d.]+%£([\d,.]+)\s*$",
        re.MULTILINE)
    for m in pat.finditer(text):
        out[m.group(1)] = (_num(m.group(2)), _num(m.group(3)))
    if not out:
        sys.exit("Could not parse the investment summary (closing position).")
    return out


def parse_contributions(text: str) -> list[dict]:
    """'SL Managed P   15 Apr 2005   £323.33   301.380 at\\n 107.3p'"""
    pat = re.compile(
        r"^\s*(SL [\w \d]+?)\s{2,}(\d{1,2}) (\w{3}) (\d{4})\s{2,}£([\d,.]+)\s{2,}"
        r"([\d,.]+) at\s*\n\s*([\d.]+)p", re.MULTILINE)
    return [{
        "date": date(int(m[4]), SHORT[m[3]], int(m[2])),
        "fund": m[1].strip(),
        "amount": _num(m[5]),
        "units": _num(m[6]),
        "price": _num(m[7]) / 100,             # pence → £
    } for m in pat.finditer(text)]


def parse_switches(text: str) -> list[dict]:
    """Each 'Fund switch on <date>' block → {date, sold, bought, total}."""
    parts = re.split(r"Fund switch on (\d{1,2}) (\w+) (\d{4})", text)[1:]
    out: list[dict] = []

    for i in range(0, len(parts), 4):
        when = date(int(parts[i + 2]), MONTHS[parts[i + 1]], int(parts[i]))
        body = parts[i + 3].split("Fund switch on")[0]
        body = re.sub(r"^.*(Page \d+|Employer|Plan number|Charges).*$", "",
                      body, flags=re.MULTILINE)      # page furniture lands mid-block

        total = _num(re.search(r"£([\d,.]+)", body).group(1))

        # Two side-by-side columns, each fund contributing a name line then a
        # quantity line, and either column may be blank on a given pair:
        #
        #   SL Managed P            SL Managed P            ← names
        #   sold 2202.631 units     bought 2165.922 units   ← quantities
        #                           SL MAMgd2060 P
        #                           bought 42.307 units
        #
        # Everything keys off the CHARACTER COLUMN, never off match order: when
        # both columns name the same fund (the common case!) order tells us
        # nothing, and pairing by order silently attributes the buy leg's units
        # to the sell leg. The split column comes from the header rather than a
        # constant, because the tables shift horizontally between pages.
        hdr = re.search(r"^(.*?)Funds switched to", body, re.MULTILINE)
        split = len(hdr.group(1)) if hdr else 48

        sold: dict[str, Decimal] = {}
        bought: dict[str, Decimal] = {}
        pending: dict[str, str | None] = {"L": None, "R": None}

        for line in body.split("\n"):
            if not line.strip():
                continue
            quantities = list(QTY_RE.finditer(line))
            if quantities:
                for m in quantities:
                    side = "L" if m.start() < split else "R"
                    fund = pending[side]
                    if fund is None:
                        sys.exit(f"{when}: units with no fund name: {line!r}")
                    (sold if m[1] == "sold" else bought)[fund] = Decimal(m[2])
                    pending[side] = None
                continue
            for m in FUND_RE.finditer(line):
                pending["L" if m.start() < split else "R"] = m[1]

        out.append({"date": when, "sold": sold, "bought": bought, "total": total})
    return out


def is_rebrand(sw: dict) -> bool:
    """A rebrand sells only funds it does not buy back — every name changes."""
    return bool(sw["sold"]) and set(sw["sold"]).isdisjoint(sw["bought"])


def solve_prices(sw: dict) -> dict[str, Decimal]:
    """Unit prices from 'the sell leg and the buy leg are both the whole plan'."""
    funds = sorted(set(sw["sold"]) | set(sw["bought"]))
    total = sw["total"]

    if len(funds) == 1:                      # first switch: only one fund held
        f = funds[0]
        return {f: total / (sw["sold"].get(f) or sw["bought"][f])}
    if len(funds) != 2:
        raise ValueError(f"{sw['date']}: {len(funds)} funds — cannot solve prices")

    a, b = funds
    sa, sb = sw["sold"].get(a, Decimal(0)), sw["sold"].get(b, Decimal(0))
    ba, bb = sw["bought"].get(a, Decimal(0)), sw["bought"].get(b, Decimal(0))
    det = sa * bb - sb * ba
    if det == 0:
        raise ValueError(f"{sw['date']}: singular — prices not recoverable")
    return {a: total * (bb - sb) / det, b: total * (sa - ba) / det}


# ── Replay ───────────────────────────────────────────────────────────────────


def _credit(rows: list[dict], held: dict[str, Decimal], when: date, fund: str,
            selling: Decimal, price: Decimal | None) -> None:
    """Reconcile `held[fund]` up to the `selling` quantity the statement is about
    to sell, booking the difference as a policy credit (free units, zero cash).

    Guard: if we hold *nothing* of a fund the statement is selling, that is not a
    credit — it is a missing opening position, and blindly crediting it would
    invent the entire holding out of thin air. Closing reconciliation cannot
    catch that (the fabricated units still land on the right closing figure), and
    importing the result into an account that already holds them doubles the
    position. So it is a hard error, not a warning.
    """
    if held[fund] <= 0 < selling:
        sys.exit(
            f"\n{when}: the statement sells {selling:,.3f} units of {fund}, but the "
            f"replay holds none.\nThe history that bought them is outside the range "
            f"being converted.\nPass the units the account already holds:  "
            f"--hold '{fund}={selling}'\n(or drop --since to replay from the start)")

    drift = selling - held[fund]
    if abs(drift) < TOL:
        return
    row = {"date": when, "security": fund, "qty": abs(drift),
           "action": "ShrsIn" if drift > 0 else "ShrsOut",
           "memo": CREDIT_MEMO if drift > 0 else "Unit adjustment"}
    if price is not None:
        row["price"] = price
    rows.append(row)
    held[fund] += drift


def build_rows(contribs: list[dict], switches: list[dict],
               held: dict[str, Decimal], contrib_cash: bool) -> list[dict]:
    """Replay the statement into QIF rows, mutating `held` to the final position."""
    rows: list[dict] = []

    for c in contribs:
        if contrib_cash:
            rows.append({"date": c["date"], "action": "Contrib", "total": c["amount"],
                         "memo": "Pension contribution"})
        rows.append({"date": c["date"], "action": "Buy", "security": c["fund"],
                     "qty": c["units"], "price": c["price"], "total": c["amount"],
                     "memo": "Contribution invested"})
        held[c["fund"]] += c["units"]

    for sw in switches:
        when = sw["date"]

        if is_rebrand(sw):
            # Pair old→new positionally: Standard Life lists the sleeves in
            # corresponding order down the two columns, and dict insertion order
            # is parse order. Guarded by the count check, and by the closing
            # reconciliation, which a mispairing would break loudly.
            if len(sw["sold"]) != len(sw["bought"]):
                sys.exit(f"{when}: rebrand has {len(sw['sold'])} funds out and "
                         f"{len(sw['bought'])} in — cannot pair them.")
            for (old, qty_out), (new, qty_in) in zip(sw["sold"].items(),
                                                     sw["bought"].items()):
                _credit(rows, held, when, old, qty_out, None)
                rows.append({"date": when, "action": "ShrsOut", "security": old,
                             "qty": qty_out, "memo": f"Share class change → {new}"})
                rows.append({"date": when, "action": "ShrsIn", "security": new,
                             "qty": qty_in, "memo": f"Share class change ← {old}"})
                held[old] -= qty_out
                held[new] += qty_in
            continue

        prices = solve_prices(sw)

        # 1. Units we don't hold yet but the statement is about to sell: credits.
        for fund, qty in sw["sold"].items():
            _credit(rows, held, when, fund, qty, prices[fund])

        # 2. Sell the whole plan.
        proceeds = Decimal(0)
        for fund, qty in sw["sold"].items():
            amount = money(qty * prices[fund])
            proceeds += amount
            rows.append({"date": when, "action": "Sell", "security": fund,
                         "qty": qty, "price": prices[fund], "total": amount,
                         "memo": "Lifestyle phased switch"})
            held[fund] -= qty

        # 3. Buy it straight back in the new mix. The last (smallest) leg absorbs
        #    the rounding residual so the switch is exactly cash-neutral — a stray
        #    penny per switch would otherwise accumulate into the cash balance.
        legs = sorted(sw["bought"].items(), key=lambda kv: -kv[1])
        spent = Decimal(0)
        for i, (fund, qty) in enumerate(legs):
            amount = (proceeds - spent if i == len(legs) - 1
                      else money(qty * prices[fund]))
            spent += amount
            rows.append({"date": when, "action": "Buy", "security": fund,
                         "qty": qty, "price": prices[fund], "total": amount,
                         "memo": "Lifestyle phased switch"})
            held[fund] += qty

    return rows


def reconcile(held: dict[str, Decimal],
              closing: dict[str, tuple[Decimal, Decimal]]) -> bool:
    """Replayed units vs the statement's own investment summary. This is the
    check that makes the output trustworthy — every fund, to the unit."""
    ok = True
    print("\nClosing position — replayed vs statement:")
    for fund, (units, value) in sorted(closing.items()):
        got = held.get(fund, Decimal(0))
        good = abs(got - units) < TOL
        ok &= good
        print(f"  {'ok  ' if good else 'FAIL'}  {fund:<16} "
              f"{got:>12,.3f} vs {units:>12,.3f} units   £{value:>9,.2f}")

    for fund, qty in sorted(held.items()):
        if fund not in closing and abs(qty) > TOL:
            print(f"  FAIL  {fund:<16} {qty:>12,.3f} units held, but the statement's "
                  f"summary does not list this fund")
            ok = False

    total = sum(v for _, v in closing.values())
    print(f"\n  Plan value: £{total:,.2f}")
    return ok


# ── Output ───────────────────────────────────────────────────────────────────


def write_qif(rows: list[dict], account: str, path: Path) -> None:
    funds = sorted({r["security"] for r in rows if "security" in r})

    out: list[str] = ["!Type:Security"]
    for f in funds:
        # No ticker: these are an insurer's internal funds, not listed anywhere,
        # so Tiingo can never price them. `symbol` empty ⇒ manual pricing, and the
        # app seeds a price from each trade for exactly this case.
        out += [f"N{f}", "S", "TMutual Fund", "^"]
    out += ["!Account", f"N{account}", "TInvst", "^", "!Type:Invst"]

    order = {"Contrib": 0, "ShrsIn": 1, "ShrsOut": 1, "Sell": 2, "Buy": 3}
    for r in sorted(rows, key=lambda r: (r["date"], order[r["action"]])):
        # QIF dates are US M/D/Y — qif_parser._parse_qif_date unpacks them as
        # (month, day, year). Emitting D/M/Y here drops every row with a day > 12
        # and, worse, silently transposes the rest.
        out.append(r["date"].strftime("D%m/%d/%Y"))
        out.append("N" + r["action"])
        if "security" in r:
            out.append("Y" + r["security"])
            out.append(f"Q{r['qty']}")
            if "price" in r:                  # absent on the rebrand legs
                out.append(f"I{r['price']:.6f}")
        if "total" in r:
            out.append(f"T{money(r['total'])}")
        out.append("M" + r["memo"])
        out.append("^")

    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"  {path}  ({len(rows)} transactions, {len(funds)} funds)")


def write_prices(switches: list[dict], closing: dict[str, tuple[Decimal, Decimal]],
                 as_of: date, path: Path) -> None:
    rows = [(f, sw["date"].isoformat(), p)
            for sw in switches if not is_rebrand(sw)
            for f, p in solve_prices(sw).items()]
    rows += [(f, as_of.isoformat(), value / units)
             for f, (units, value) in closing.items()]

    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")   # not csv's default CRLF
        w.writerow(["security", "date", "price_gbp"])
        w.writerows((f, d, f"{p:.6f}") for f, d, p in sorted(rows))
    print(f"  {path}  ({len(rows)} unit prices)")


def load_prices(db: Path, price_csv: Path) -> None:
    """Write the derived prices into `security_price` as source='manual'.

    The app has no price-import path (only the Stock Record dialog's one-at-a-time
    entry), and nobody is hand-typing 74 prices. Requires the securities to exist,
    so import the QIF first. Close the app before running this.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from mfl_desktop.db.repository import Repository       # noqa: E402

    repo = Repository(str(db))
    securities = {s.name: s.id for s in repo.list_securities()}

    loaded = 0
    with price_csv.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            sid = securities.get(row["security"])
            if sid is None:
                sys.exit(f"Security {row['security']!r} not in {db.name} — "
                         "import the QIF before loading prices.")
            repo.upsert_security_price(security_id=sid, price_date=row["date"],
                                       price=float(row["price_gbp"]),
                                       source="manual", currency="GBP")
            loaded += 1
    print(f"  loaded {loaded} prices into {db}")


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Convert a Standard Life pension statement PDF to QIF.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="The QIF imports through the app's normal Import flow. The price "
               "CSV has no importer — use --load-prices, with the app closed.")
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--account", default="Standard Life Pension",
                    help="account name written into the QIF (default: %(default)s)")
    ap.add_argument("--out-dir", type=Path,
                    help="where to write (default: alongside the PDF)")
    ap.add_argument("--since", metavar="YYYY-MM-DD",
                    help="skip activity before this date. For a follow-up statement "
                         "that re-lists history you already imported — WITHOUT this, "
                         "re-importing double-counts, as these rows carry no "
                         "provider IDs to dedupe on. Seed the position you already "
                         "hold with --hold, or reconciliation will fail.")
    ap.add_argument("--hold", action="append", metavar="FUND=UNITS", default=[],
                    help="units already held when --since starts. Repeatable.")
    ap.add_argument("--contrib-cash", action="store_true",
                    help="also emit a Contrib row funding each Buy. For an EMPTY "
                         "account. If the contributions are already in the account "
                         "as cash rows, this doubles the money and the unit "
                         "reconciliation will NOT catch it.")
    ap.add_argument("--load-prices", type=Path, metavar="DB",
                    help="after writing, load the prices into this .mfl "
                         "(app must be closed; import the QIF first)")
    args = ap.parse_args(argv)

    if not args.pdf.exists():
        return print(f"No such file: {args.pdf}") or 1

    text = read_pdf(args.pdf)
    closing = parse_closing(text)
    contribs = parse_contributions(text)
    switches = parse_switches(text)

    as_of = max(sw["date"] for sw in switches) if switches else date.today()
    if m := re.search(r"Investment summary on (\d{1,2}) (\w+) (\d{4})", text):
        as_of = date(int(m[3]), MONTHS[m[2]], int(m[1]))

    if args.since:
        cutoff = date.fromisoformat(args.since)
        contribs = [c for c in contribs if c["date"] >= cutoff]
        switches = [s for s in switches if s["date"] >= cutoff]

    held: dict[str, Decimal] = defaultdict(Decimal)
    for spec in args.hold:
        fund, _, units = spec.partition("=")
        held[fund.strip()] = Decimal(units.strip())

    print(f"Parsed {args.pdf.name}:")
    print(f"  {len(contribs)} contributions  £{sum(c['amount'] for c in contribs):,.2f}")
    print(f"  {len(switches)} switches"
          f"{f' ({sum(map(is_rebrand, switches))} share-class rebrand)' if any(map(is_rebrand, switches)) else ''}")
    print(f"  closing position on {as_of}: {', '.join(sorted(closing))}")

    rows = build_rows(contribs, switches, held, args.contrib_cash)

    if not reconcile(held, closing):
        print("\nDoes NOT reconcile against the statement — nothing written.\n"
              "If you used --since, pass the units already held with --hold.",
              file=sys.stderr)
        return 1

    out_dir = args.out_dir or args.pdf.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = args.account.replace(" ", "_").lower()
    qif_path = out_dir / f"{stem}.qif"
    csv_path = out_dir / f"{stem}-prices.csv"

    print("\nWrote:")
    write_qif(rows, args.account, qif_path)
    write_prices(switches, closing, as_of, csv_path)

    if args.load_prices:
        load_prices(args.load_prices, csv_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
