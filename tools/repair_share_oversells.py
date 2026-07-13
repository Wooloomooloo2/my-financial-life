#!/usr/bin/env python3
"""Repair share-oversells and the phantom holdings they leave behind (ADR-155).

Operator tool, not shipped with the app.

WHAT IT REPAIRS
---------------
The holdings engine drains FIFO lots with ``while remaining > _EPS and queue``:
a sell bigger than the holding **stops at zero and silently discards the
excess**. Nothing raises. Two consequences follow, and this tool fixes both:

1. **Overstated realised gain.** The unbacked shares are sold with no cost
   matched against them, so their whole proceeds book as gain.

2. **Phantom holdings.** The data went *negative* where the engine clamped at
   *zero*, so every later share-in sits on top of a floor the engine never
   applied. A compensating ``ShrsIn`` "plug" — the shape brokers and importers
   produce to force the running total back to zero — then materialises shares
   out of nothing. That phantom is what shows up on the Securities screen as a
   holding of 0.002 shares you never owned.

THE REPAIR
----------
For a *rounding* oversell (a broker closing a position and rounding the share
count), the truth is that the sale sold everything and no more. So:

  - trim the offending sell's quantity down to exactly what was held, and
  - delete the compensating share-in plug, if one is present.

The **cash amount is never touched** — that came off the statement and is
real. Only the share count moves, which is what makes the realised gain correct
(the full basis is now matched against the true proceeds) and lands the position
on exactly zero.

WHAT IT WILL NOT DO
-------------------
A *large* oversell is not rounding — it means history is missing (a buy or a
transfer that was never imported), and trimming the sale to fit would rewrite
your statement to match a gap in the data. Those are reported and skipped
unless you name them explicitly with --add-shares, which inserts the missing
shares *before* the sale instead, leaving the sale exactly as the broker
recorded it.

    # See what would change; writes nothing.
    python tools/repair_share_oversells.py FILE.mfl

    # Apply the rounding repairs (<= --max-rounding shares).
    python tools/repair_share_oversells.py FILE.mfl --apply

    # Also fix a named large one by inserting the shares that are missing.
    python tools/repair_share_oversells.py FILE.mfl --apply \\
        --add-shares "MS Access - Maria:DSI"

Always takes a .backup snapshot before writing, and refuses to commit unless
every repaired position replays to exactly zero.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop.db.repository import new_transaction_iri  # noqa: E402
from mfl_desktop.import_engine.qif_actions import (  # noqa: E402
    is_share_in, is_share_out, is_split,
)

# A sell that overshoots by no more than this is a broker rounding a
# "sell everything" instruction — safe to trim to the true holding. Anything
# larger implies missing history and is reported, never guessed at.
DEFAULT_MAX_ROUNDING = 0.5


@dataclass
class Oversell:
    account_id: int
    account: str
    security_id: int
    security: str
    txn_id: int
    date: str
    action: str
    quantity: float
    held: float
    # Every compensating share-in that cancels this oversell. Usually one, but
    # the same fractional adjustment can arrive on two statements and be
    # imported twice — PDBC carries two ShrsIn 0.001 rows a year apart. Delete
    # one and the position still won't reach zero.
    plugs: list[tuple[int, float]] = field(default_factory=list)
    max_rounding: float = DEFAULT_MAX_ROUNDING

    @property
    def excess(self) -> float:
        return self.quantity - self.held

    @property
    def is_rounding(self) -> bool:
        return self.excess <= self.max_rounding


def _rows(conn, sql, args=()):
    conn.row_factory = sqlite3.Row
    return conn.execute(sql, args).fetchall()


def find_oversells(conn, max_rounding: float) -> list[Oversell]:
    """Replay every investment account and flag each share-out that exceeds the
    shares held at that moment — mirroring the engine's clamp, so what we find
    is what the app is actually showing."""
    out: list[Oversell] = []
    accounts = _rows(
        conn,
        "SELECT id, name FROM account WHERE family = 'investment' "
        "ORDER BY name",
    )
    for a in accounts:
        txns = _rows(
            conn,
            "SELECT t.id, t.posted_date, t.action, t.quantity, t.security_id, "
            "       COALESCE(s.symbol, s.name) AS sec "
            "FROM txn t JOIN security s ON s.id = t.security_id "
            "WHERE t.account_id = ? AND t.security_id IS NOT NULL "
            "  AND t.action IS NOT NULL AND t.quantity IS NOT NULL "
            "ORDER BY t.posted_date, t.id",
            (a["id"],),
        )
        held: dict[int, float] = {}
        for t in txns:
            sid = t["security_id"]
            qty = float(t["quantity"] or 0.0)
            action = t["action"] or ""
            if is_split(action):
                held[sid] = held.get(sid, 0.0) * (qty or 1.0)
                continue
            if is_share_in(action):
                held[sid] = held.get(sid, 0.0) + qty
            elif is_share_out(action):
                have = held.get(sid, 0.0)
                if qty > have + 1e-9:
                    out.append(Oversell(
                        account_id=a["id"], account=a["name"],
                        security_id=sid, security=t["sec"],
                        txn_id=t["id"], date=t["posted_date"],
                        action=action, quantity=qty, held=have,
                        max_rounding=max_rounding,
                    ))
                # The engine clamps here; so do we, or every later row in this
                # security would be measured against a floor it never used.
                held[sid] = max(0.0, have - qty)
    for o in out:
        _find_plugs(conn, o)
    return out


def _find_plugs(conn, o: Oversell) -> None:
    """Locate the compensating share-ins that cancel an oversell in the raw
    data: same account + security, on or after the sale, quantity equal to the
    excess. Such a row exists only to paper over the oversell — and because the
    engine clamped the oversell to zero, it is what becomes a phantom holding.

    Collects *all* of them, not the first: a fractional adjustment re-imported
    from a second statement leaves duplicates, and a half-repair is worse than
    none (the position still won't reach zero, and the guard would then reject
    the whole run)."""
    for r in _rows(
        conn,
        "SELECT id, quantity, action FROM txn "
        "WHERE account_id = ? AND security_id = ? AND posted_date >= ? "
        "  AND action IS NOT NULL AND quantity IS NOT NULL "
        "ORDER BY posted_date, id",
        (o.account_id, o.security_id, o.date),
    ):
        if not is_share_in(r["action"] or ""):
            continue
        if abs(float(r["quantity"] or 0.0) - o.excess) < 1e-6:
            o.plugs.append((r["id"], float(r["quantity"])))


def held_after_replay(conn, account_id: int, security_id: int) -> float:
    """Shares the engine would show for one security in one account, applying
    the same oversell clamp."""
    held = 0.0
    for t in _rows(
        conn,
        "SELECT action, quantity FROM txn WHERE account_id = ? "
        "AND security_id = ? AND action IS NOT NULL AND quantity IS NOT NULL "
        "ORDER BY posted_date, id",
        (account_id, security_id),
    ):
        action, qty = t["action"] or "", float(t["quantity"] or 0.0)
        if is_split(action):
            held *= qty or 1.0
        elif is_share_in(action):
            held += qty
        elif is_share_out(action):
            held = max(0.0, held - qty)
    return held


def _backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(f"{path.stem}.pre-oversell-repair-{stamp}{path.suffix}")
    shutil.copy2(path, dest)
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", type=Path, help="the .mfl file")
    ap.add_argument("--apply", action="store_true",
                    help="write the repairs (default: dry run)")
    ap.add_argument("--max-rounding", type=float, default=DEFAULT_MAX_ROUNDING,
                    help=f"largest oversell treated as rounding "
                         f"(default {DEFAULT_MAX_ROUNDING} shares)")
    ap.add_argument("--add-shares", action="append", default=[],
                    metavar="ACCOUNT:SECURITY",
                    help="for a large oversell, insert the missing shares "
                         "before the sale instead of trimming it")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"error: {args.db} not found", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    oversells = find_oversells(conn, args.max_rounding)
    if not oversells:
        print("No oversells found — nothing to repair.")
        return 0

    add = {s.strip() for s in args.add_shares}
    trim = [o for o in oversells if o.is_rounding]
    insert = [o for o in oversells
              if not o.is_rounding and f"{o.account}:{o.security}" in add]
    skip = [o for o in oversells if not o.is_rounding and o not in insert]

    print(f"{len(oversells)} oversell(s) found in {args.db.name}\n")
    print("TRIM the sale to the shares actually held "
          f"(rounding, <= {args.max_rounding} sh):")
    for o in trim or []:
        plug = (
            ", delete plug txn " + ", ".join(
                f"{pid} (+{q:g} sh)" for pid, q in o.plugs
            ) if o.plugs else ""
        )
        print(f"  {o.account:22.22} {o.security:14.14} {o.date}  {o.action:7.7} "
              f"{o.quantity:>12.6f} → {o.held:<12.6f} (over by {o.excess:.6f}){plug}")
    if not trim:
        print("  (none)")

    if insert:
        print("\nINSERT the missing shares before the sale (sale left as-is):")
        for o in insert:
            print(f"  {o.account:22.22} {o.security:14.14} {o.date}  "
                  f"+{o.excess:.6f} sh as ShrsIn, then {o.action} {o.quantity:g} stands"
                  + ("; delete plug txn "
                     + ", ".join(str(pid) for pid, _ in o.plugs)
                     if o.plugs else ""))

    if skip:
        print("\nSKIPPED — too large to be rounding; history is missing. "
              "Re-run with --add-shares 'Account:SECURITY' to insert the "
              "shortfall, or fix by hand:")
        for o in skip:
            print(f"  {o.account:22.22} {o.security:14.14} {o.date}  {o.action:7.7} "
                  f"sold {o.quantity:g} holding {o.held:g} — over by {o.excess:g}")

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply to make these changes.")
        return 0

    backup = _backup(args.db)
    print(f"\nBackup: {backup.name}")

    touched: set[tuple[int, int]] = set()
    for o in trim:
        conn.execute("UPDATE txn SET quantity = ? WHERE id = ?", (o.held, o.txn_id))
        for plug_id, _q in o.plugs:
            conn.execute("DELETE FROM txn WHERE id = ?", (plug_id,))
        touched.add((o.account_id, o.security_id))
    for o in insert:
        # Dated the day before the sale so the replay has the shares in hand.
        day_before = (
            datetime.strptime(o.date, "%Y-%m-%d").toordinal() - 1
        )
        prior = datetime.fromordinal(day_before).strftime("%Y-%m-%d")
        conn.execute(
            "INSERT INTO txn (iri, account_id, posted_date, amount, category_id, "
            "                 status, memo, action, security_id, quantity) "
            "SELECT ?, ?, ?, 0, "
            "  (SELECT id FROM category WHERE name = 'Uncategorised' LIMIT 1), "
            "  'cleared', ?, 'ShrsIn', ?, ?",
            (new_transaction_iri(), o.account_id, prior,
             f"ADR-155 repair: shares missing from import, implied by the "
             f"{o.date} {o.action} of {o.quantity:g}",
             o.security_id, o.excess),
        )
        for plug_id, _q in o.plugs:
            conn.execute("DELETE FROM txn WHERE id = ?", (plug_id,))
        touched.add((o.account_id, o.security_id))

    # Guard: every repaired position must now replay to exactly zero (the sale
    # closed it) — if it doesn't, the repair was wrong and nothing is committed.
    bad = []
    for account_id, security_id in touched:
        left = held_after_replay(conn, account_id, security_id)
        if left > 1e-6:
            bad.append((account_id, security_id, left))
    if bad:
        conn.rollback()
        print("\nREFUSED — a repaired position does not replay to zero:")
        for account_id, security_id, left in bad:
            print(f"  account {account_id} security {security_id}: {left:.6f} sh left")
        print("Nothing was written. Your file is unchanged.")
        return 1

    conn.commit()
    print(f"\nRepaired {len(trim)} by trimming, {len(insert)} by inserting shares. "
          f"{len(skip)} left alone.")
    print("Every repaired position now replays to exactly zero.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
