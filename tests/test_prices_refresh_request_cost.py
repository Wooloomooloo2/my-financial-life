"""Tiingo latest-price refresh costs zero requests when nothing is new
(ADR-049 amendment).

Tiingo's daily endpoint is one request per ticker and the free tier allows only
**50 per hour**. A 32-ticker portfolio therefore burns 32 requests per sweep, so
the launch refresh plus one click of *Update prices* used to exceed the cap — even
on a Sunday, when every stored close was already current and not one of those 64
requests could return anything new.

The fix: skip any security whose newest *market* price already reaches the latest
published close. These tests count the actual HTTP calls the refresh makes.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop import prices
from mfl_desktop.db.repository import Repository


class _CountingClient:
    """Stands in for TiingoClient and counts the tickers actually requested —
    the thing the free tier bills. ``served`` is the close it returns."""

    calls: list[str] = []

    def __init__(self, served: str = "2026-07-10", price: float = 100.0):
        self._served = served
        self._price = price

    def __call__(self, api_key, *a, **kw):     # constructed as TiingoClient(key)
        return self

    def fetch_latest(self, symbols):
        syms = [s.strip().upper() for s in symbols if (s or "").strip()]
        _CountingClient.calls.extend(syms)
        return {s: (self._price, self._served) for s in syms}, []


def _install(monkey_served="2026-07-10"):
    """Point prices.TiingoClient at the counter; return a reset call log."""
    _CountingClient.calls = []
    prices.TiingoClient = _CountingClient(served=monkey_served)  # type: ignore[assignment]
    return _CountingClient.calls


def _repo(n_tickers: int = 3, priced_through: str | None = None):
    """A file with `n_tickers` held, tickered securities. When `priced_through`
    is set, each already has a tiingo close on that date."""
    db = Path(tempfile.mkdtemp(prefix="mfl_px_")) / "m.mfl"
    repo = Repository(db)
    repo.set_setting("tiingo_api_key", "TESTKEY")
    repo.create_account(
        name="Brokerage", type_key="investment", currency="USD",
        opening_balance=Decimal("0"),
    )
    acct = repo.list_accounts()[0]
    for i in range(n_tickers):
        sid = repo.get_or_create_security(f"Fund {i}", f"TIC{i}")
        # securities_to_price only returns securities that are actually held.
        repo.insert_transaction(
            account_id=acct.id, posted_date="2026-01-05",
            amount=Decimal("-100.00"), payee_id=None,
            category_id=repo.uncategorised_id(), status="pending", memo="",
            import_hash=None, import_batch_id=None,
            action="Buy", security_id=sid,
            quantity=Decimal("1"), price=Decimal("100"),
        )
        if priced_through:
            repo.upsert_security_price(
                security_id=sid, price_date=priced_through,
                price=100.0, source="tiingo",
            )
    repo.commit()
    return repo


_SUNDAY = datetime(2026, 7, 12, 9, 0, tzinfo=timezone.utc)     # the reported bug
_FRIDAY_PM = datetime(2026, 7, 10, 23, 0, tzinfo=timezone.utc)  # after the close
_MONDAY_AM = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)   # before the close


# ── which close a refresh is even trying to reach ────────────────────────────


def test_weekend_targets_fridays_close():
    assert prices.expected_close_date(_SUNDAY) == "2026-07-10"
    # Monday morning, before the US close is published — still Friday.
    assert prices.expected_close_date(_MONDAY_AM) == "2026-07-10"
    # Friday night, after it: Friday itself.
    assert prices.expected_close_date(_FRIDAY_PM) == "2026-07-10"
    # Monday night, after the close: Monday.
    assert prices.expected_close_date(
        datetime(2026, 7, 13, 23, 0, tzinfo=timezone.utc)
    ) == "2026-07-13"


# ── the bug: a sweep that can't return anything must not be sent ─────────────


def test_refresh_when_already_current_makes_no_requests():
    repo = _repo(n_tickers=3, priced_through="2026-07-10")
    calls = _install()
    r = prices.refresh_latest_prices_into(repo, force=True, now=_SUNDAY)
    assert calls == []                      # THE point: zero Tiingo requests
    assert r.new_prices_count == 0
    assert r.skipped_count == 3
    assert r.errors == []


def test_stale_securities_still_fetch():
    repo = _repo(n_tickers=3, priced_through="2026-06-01")
    calls = _install()
    r = prices.refresh_latest_prices_into(repo, force=True, now=_SUNDAY)
    assert sorted(calls) == ["TIC0", "TIC1", "TIC2"]
    assert r.new_prices_count == 3
    assert r.skipped_count == 0


def test_only_the_stale_ones_fetch():
    repo = _repo(n_tickers=0)
    acct = repo.list_accounts()[0]
    for i, on_date in enumerate(("2026-07-10", "2026-06-01")):
        sid = repo.get_or_create_security(f"Fund {i}", f"TIC{i}")
        repo.insert_transaction(
            account_id=acct.id, posted_date="2026-01-05", amount=Decimal("-100"),
            payee_id=None, category_id=repo.uncategorised_id(), status="pending",
            memo="", import_hash=None, import_batch_id=None, action="Buy",
            security_id=sid, quantity=Decimal("1"), price=Decimal("100"),
        )
        repo.upsert_security_price(
            security_id=sid, price_date=on_date, price=100.0, source="tiingo",
        )
    repo.commit()
    calls = _install()
    r = prices.refresh_latest_prices_into(repo, force=True, now=_SUNDAY)
    assert calls == ["TIC1"]                # the current one is not requested
    assert r.skipped_count == 1


def test_second_refresh_in_the_same_hour_is_free():
    """The reported failure: launch refresh + a click of Update prices = 2 × N
    requests, which blows the 50/hour free tier. The second sweep now costs 0."""
    repo = _repo(n_tickers=3, priced_through="2026-06-01")
    calls = _install()
    prices.refresh_latest_prices_into(repo, force=True, now=_SUNDAY)
    assert len(calls) == 3                  # first sweep does the work
    calls2 = _install()
    r = prices.refresh_latest_prices_into(repo, force=True, now=_SUNDAY)
    assert calls2 == []                     # ...the second asks for nothing
    assert r.skipped_count == 3


# ── a transaction-seeded price is not a close ────────────────────────────────


def test_transaction_seeded_price_does_not_suppress_the_fetch():
    """seed_prices_from_transactions writes a price row from the owner's own
    trade print. That's not the day's close, so it must not make the security
    look current — otherwise a stock bought on Friday never gets a real price."""
    repo = _repo(n_tickers=1)
    sid = repo.list_securities()[0].id
    repo.upsert_security_price(
        security_id=sid, price_date="2026-07-10", price=100.0,
        source="transaction",
    )
    repo.commit()
    calls = _install()
    prices.refresh_latest_prices_into(repo, force=True, now=_SUNDAY)
    assert calls == ["TIC0"]                # fetched despite the seeded row


# ── holidays are learned, not calendared ─────────────────────────────────────


def test_holiday_repeat_refresh_is_free():
    """On a day the calendar calls a trading day but the market was shut, the
    first sweep learns the market's newest close is older than expected — and
    every later refresh that day then costs nothing instead of re-sweeping."""
    repo = _repo(n_tickers=3, priced_through="2026-06-01")
    # Expect Friday 07-10's close, but the market only ever closed on 07-09.
    calls = _install(monkey_served="2026-07-09")
    prices.refresh_latest_prices_into(repo, force=True, now=_SUNDAY)
    assert len(calls) == 3                  # first sweep pays, and learns
    assert repo.get_setting("tiingo_market_close_date") == "2026-07-09"

    calls2 = _install(monkey_served="2026-07-09")
    r = prices.refresh_latest_prices_into(repo, force=True, now=_SUNDAY)
    assert calls2 == []                     # no re-sweep for a close that isn't coming
    assert r.skipped_count == 3


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    real_client = prices.TiingoClient
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
        finally:
            prices.TiingoClient = real_client
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
