"""Tiingo security-price client + refresh helpers (ADR-044).

Pure Python — no Qt imports, no threading; callers wrap the synchronous
``refresh_latest_prices_into`` in a worker if they want it non-blocking
(``__main__`` does this on launch). Deliberately mirrors ``fx.py``'s shape so
the two external-data integrations read the same.

Tiingo (https://www.tiingo.com) covers US stocks, ETFs, and mutual funds with a
free API key. Endpoint used:

  - ``GET /tiingo/daily/<ticker>/prices?token=KEY`` — latest end-of-day row
    (``[{ "date": "...", "close": <price>, ... }]``)
  - (round 3) the same path with ``&startDate=&endDate=`` for history.

Only securities that carry a ticker ``symbol`` can be fetched; the rest are
manual-price only (Manage ▸ Securities). The ``setting`` table holds the API
key (``tiingo_api_key``) and the last-successful-refresh timestamp
(``tiingo_last_refresh_at``). Every row this module writes has ``source='tiingo'``.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable, Optional

if TYPE_CHECKING:
    from mfl_desktop.db.repository import Repository

logger = logging.getLogger(__name__)

# Skip the launch refresh if the last one was less than this many hours ago.
# 24h matches "we only need today's close" personal-finance scope.
LAUNCH_REFRESH_INTERVAL_HOURS = 24

_BASE_URL = "https://api.tiingo.com/tiingo/daily"


class PriceFetchError(RuntimeError):
    """Raised when the provider call fails or returns an error payload.
    Distinct from ValueError so the Securities dialog can show "couldn't reach
    Tiingo" without conflating it with user-typed bad input."""


@dataclass(frozen=True)
class RefreshResult:
    """Summary returned by the refresh helpers. ``new_prices_count`` is how many
    ``security_price`` rows were upserted; ``errors`` is human-readable strings
    suitable for a small dialog log box."""
    fetched_at: Optional[str]
    new_prices_count: int
    errors: list[str]


class TiingoClient:
    """Thin urllib wrapper around the Tiingo daily-prices endpoint."""

    def __init__(self, api_key: str, timeout_seconds: float = 15.0) -> None:
        self._api_key = (api_key or "").strip()
        self._timeout = timeout_seconds

    def fetch_latest(
        self, symbols: Iterable[str],
    ) -> tuple[dict[str, tuple[float, str]], list[str]]:
        """Latest end-of-day close per symbol.

        Returns ``({SYMBOL: (price, 'YYYY-MM-DD')}, errors)``. Tiingo's daily
        endpoint is one ticker per call, so an unknown/unsupported symbol fails
        that symbol only — its message is collected in ``errors`` and the rest
        continue (one bad fund ticker shouldn't sink the whole refresh)."""
        if not self._api_key:
            raise PriceFetchError(
                "No Tiingo API key set. Add one in Manage ▸ Securities."
            )
        out: dict[str, tuple[float, str]] = {}
        errors: list[str] = []
        seen: set[str] = set()
        for raw in symbols:
            sym = (raw or "").strip().upper()
            if not sym or sym in seen:
                continue
            seen.add(sym)
            try:
                price, on_date = self._fetch_one(sym)
                if price is not None:
                    out[sym] = (price, on_date)
                else:
                    errors.append(f"{sym}: no price returned")
            except PriceFetchError as e:
                errors.append(str(e))
        return out, errors

    def fetch_historical(
        self, symbol: str, start_date: Optional[str] = None,
    ) -> list[tuple[str, float]]:
        """Full daily close series for one symbol, ascending by date. Tiingo
        returns the whole history in a single call, so a backfill is one
        request per ticker. ``start_date`` ('YYYY-MM-DD') bounds the earliest
        day; None lets Tiingo return its default window (several years)."""
        sym = (symbol or "").strip().upper()
        if not sym:
            return []
        payload = self._request(sym, start_date=start_date)
        out: list[tuple[str, float]] = []
        for row in payload:
            close = row.get("close")
            on_date = str(row.get("date", ""))[:10]
            if close is not None and on_date:
                out.append((on_date, float(close)))
        return out

    def _fetch_one(self, symbol: str) -> tuple[Optional[float], str]:
        payload = self._request(symbol, start_date=None)
        if not payload:
            return None, ""
        last = payload[-1]  # endpoint returns ascending; last row is most recent
        close = last.get("close")
        on_date = str(last.get("date", ""))[:10]
        if close is None:
            return None, on_date
        return float(close), on_date

    def _request(self, symbol: str, *, start_date: Optional[str]) -> list:
        params = {"token": self._api_key, "format": "json", "resampleFreq": "daily"}
        if start_date:
            params["startDate"] = start_date
        url = (
            f"{_BASE_URL}/{urllib.parse.quote(symbol)}/prices"
            f"?{urllib.parse.urlencode(params)}"
        )
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
                msg = body.get("detail") or body.get("message") or str(e)
            except Exception:
                msg = str(e)
            raise PriceFetchError(f"{symbol}: {msg}") from e
        except urllib.error.URLError as e:
            raise PriceFetchError(
                f"Could not reach Tiingo for {symbol}: {e.reason}"
            ) from e
        return payload if isinstance(payload, list) else []


def _hours_since(iso_ts: str) -> Optional[float]:
    """Hours between ``iso_ts`` and now (UTC); ``None`` when empty/unparseable
    (treated as 'stale, needs refresh')."""
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0


def refresh_latest_prices_into(
    repo: "Repository", *, force: bool = False,
) -> RefreshResult:
    """Fetch the latest close for every tickered security and upsert into
    ``security_price``.

    No-op when: the API key is unset; the last refresh was < 24h ago and
    ``force=False`` (launch path); or no securities carry a symbol. Records the
    refresh timestamp on success; failures leave it untouched so the next
    launch retries.
    """
    api_key = repo.get_setting("tiingo_api_key")
    if not api_key:
        return RefreshResult(
            fetched_at=None, new_prices_count=0,
            errors=["No Tiingo API key set."],
        )
    last = repo.get_setting("tiingo_last_refresh_at") or ""
    if not force:
        hrs = _hours_since(last)
        if hrs is not None and hrs < LAUNCH_REFRESH_INTERVAL_HOURS:
            return RefreshResult(fetched_at=last, new_prices_count=0, errors=[])

    securities = repo.list_securities_with_symbol()
    if not securities:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        repo.set_setting("tiingo_last_refresh_at", now)
        return RefreshResult(fetched_at=now, new_prices_count=0, errors=[])

    # symbol (upper) → security ids that use it (a symbol can, rarely, map to
    # more than one mastered security).
    by_symbol: dict[str, list[int]] = {}
    for s in securities:
        by_symbol.setdefault(s.symbol.strip().upper(), []).append(s.id)

    client = TiingoClient(api_key)
    try:
        prices, errors = client.fetch_latest(by_symbol.keys())
    except PriceFetchError as e:
        return RefreshResult(
            fetched_at=last or None, new_prices_count=0, errors=[str(e)],
        )

    count = 0
    for symbol, (price, on_date) in prices.items():
        if not on_date:
            continue
        for sid in by_symbol.get(symbol, []):
            try:
                repo.upsert_security_price(
                    security_id=sid, price_date=on_date,
                    price=price, source="tiingo",
                )
                count += 1
            except Exception as e:  # noqa: BLE001 — collect, don't abort the batch
                errors.append(f"{symbol}: {e}")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    repo.set_setting("tiingo_last_refresh_at", now)
    return RefreshResult(fetched_at=now, new_prices_count=count, errors=errors)


def backfill_historical_into(
    repo: "Repository", *, start_date: Optional[str] = None, on_progress=None,
) -> RefreshResult:
    """Fetch each tickered security's full daily history and upsert it into
    ``security_price`` (source='tiingo'). One Tiingo call per ticker — cheap.
    Idempotent (ON CONFLICT), so re-running just refreshes. Per-symbol errors
    (a fund Tiingo doesn't cover) are collected and the loop continues.

    ``on_progress(done, total)`` is called per security so a dialog can show a
    bar. Raises PriceFetchError only if the key is missing."""
    api_key = repo.get_setting("tiingo_api_key")
    if not api_key:
        raise PriceFetchError(
            "No Tiingo API key set. Add one in Manage ▸ Securities first."
        )
    securities = repo.list_securities_with_symbol()
    if not securities:
        return RefreshResult(fetched_at=None, new_prices_count=0, errors=[])

    client = TiingoClient(api_key)
    errors: list[str] = []
    count = 0
    total = len(securities)
    for i, sec in enumerate(securities):
        try:
            series = client.fetch_historical(sec.symbol, start_date)
            rows = [
                (sec.id, on_date, price, "tiingo") for on_date, price in series
            ]
            if rows:
                repo.bulk_upsert_security_prices(rows)
                count += len(rows)
        except PriceFetchError as e:
            errors.append(str(e))
        except Exception as e:  # noqa: BLE001 — collect, keep going
            errors.append(f"{sec.symbol}: {e}")
        if on_progress is not None:
            try:
                on_progress(i + 1, total)
            except Exception:
                pass

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    repo.set_setting("tiingo_last_refresh_at", now)
    return RefreshResult(fetched_at=now, new_prices_count=count, errors=errors)
