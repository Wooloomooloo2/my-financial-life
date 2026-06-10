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
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Iterable, Optional

if TYPE_CHECKING:
    from mfl_desktop.db.repository import Repository

logger = logging.getLogger(__name__)

# Skip the launch refresh if the last one was less than this many hours ago.
# 24h matches "we only need today's close" personal-finance scope.
LAUNCH_REFRESH_INTERVAL_HOURS = 24

# Request-waste controls (ADR-049).
# How long to stop fetching after a 429, when Tiingo gives no Retry-After. One
# hour clears the 50/hour cap and still recovers same-day for the 1000/day cap.
RATE_LIMIT_BACKOFF_SECONDS = 3600
# Don't re-fetch a security whose ticker Tiingo couldn't serve for this long; it
# is then retried automatically (a ticker that gains coverage heals itself).
PRICE_UNAVAILABLE_COOLDOWN_DAYS = 30

# Default earliest date for a history backfill (ADR-049 amendment). Tiingo's
# daily-prices endpoint returns only the latest SINGLE row when no startDate is
# sent; the full series requires an explicit startDate. This far-past date is
# clamped by Tiingo to each security's inception, so one call returns the whole
# history. Used whenever fetch_historical is called without an explicit start.
HISTORY_START_DATE = "1900-01-01"

_BASE_URL = "https://api.tiingo.com/tiingo/daily"


class PriceFetchError(RuntimeError):
    """Raised when the provider call fails or returns an error payload.
    Distinct from ValueError so the Securities dialog can show "couldn't reach
    Tiingo" without conflating it with user-typed bad input."""


class RateLimitedError(PriceFetchError):
    """Tiingo returned HTTP 429 (rate limit hit, ADR-049). ``retry_after_seconds``
    is parsed from a ``Retry-After`` header when present, else None. Callers
    persist a back-off window and stop the current run — every further call this
    run would be a guaranteed 429."""

    def __init__(self, message: str, retry_after_seconds: Optional[int] = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class SymbolNotFoundError(PriceFetchError):
    """Tiingo doesn't cover this ticker (HTTP 404 / unknown symbol, ADR-049).
    Distinct from a transient network error so the backfill loop marks the
    security 'give up for the cooldown' rather than retrying it next launch."""


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
            except RateLimitedError:
                # A 429 is fatal to the whole batch — every remaining symbol
                # would 429 too. Abort and let the caller record a back-off
                # (ADR-049) rather than burning the rest of the hour's quota.
                raise
            except PriceFetchError as e:
                errors.append(str(e))
        return out, errors

    def fetch_historical(
        self, symbol: str, start_date: Optional[str] = None,
    ) -> list[tuple[str, float]]:
        """Full daily close series for one symbol, ascending by date. Tiingo
        returns the whole history in a single call, so a backfill is one
        request per ticker. ``start_date`` ('YYYY-MM-DD') bounds the earliest
        day; when None we send ``HISTORY_START_DATE`` (a far-past date Tiingo
        clamps to inception) — **without a startDate Tiingo's prices endpoint
        returns only the latest single row, not history** (ADR-049 amendment)."""
        sym = (symbol or "").strip().upper()
        if not sym:
            return []
        payload = self._request(sym, start_date=start_date or HISTORY_START_DATE)
        out: list[tuple[str, float]] = []
        for row in payload:
            close = row.get("close")
            on_date = str(row.get("date", ""))[:10]
            if close is not None and on_date:
                out.append((on_date, float(close)))
        return out

    def fetch_metadata(self, symbol: str) -> Optional[dict]:
        """Return Tiingo's metadata object for a ticker (``{'ticker','name',
        'description',...}``) or ``None``. Powers the Stock/transaction dialogs'
        symbol→security-name auto-fill (ADR-048). One ticker per call; raises
        PriceFetchError on a missing key or provider error."""
        sym = (symbol or "").strip().upper()
        if not sym:
            return None
        if not self._api_key:
            raise PriceFetchError(
                "No Tiingo API key set. Add one in Manage ▸ Securities."
            )
        url = (
            f"{_BASE_URL}/{urllib.parse.quote(sym)}"
            f"?{urllib.parse.urlencode({'token': self._api_key, 'format': 'json'})}"
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
            if e.code == 429:
                raise RateLimitedError(
                    f"Tiingo rate limit hit: {msg}",
                    _parse_retry_after(e.headers.get("Retry-After")),
                ) from e
            if e.code == 404:
                raise SymbolNotFoundError(f"{sym}: {msg}") from e
            raise PriceFetchError(f"{sym}: {msg}") from e
        except urllib.error.URLError as e:
            raise PriceFetchError(
                f"Could not reach Tiingo for {sym}: {e.reason}"
            ) from e
        return payload if isinstance(payload, dict) else None

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
            if e.code == 429:
                raise RateLimitedError(
                    f"Tiingo rate limit hit: {msg}",
                    _parse_retry_after(e.headers.get("Retry-After")),
                ) from e
            if e.code == 404:
                raise SymbolNotFoundError(f"{symbol}: {msg}") from e
            raise PriceFetchError(f"{symbol}: {msg}") from e
        except urllib.error.URLError as e:
            raise PriceFetchError(
                f"Could not reach Tiingo for {symbol}: {e.reason}"
            ) from e
        return payload if isinstance(payload, list) else []


def _parse_retry_after(value: Optional[str]) -> Optional[int]:
    """Tiingo's ``Retry-After`` header in seconds, when it sends one. Only the
    integer-seconds form is honoured (the HTTP-date form is ignored → default
    back-off); a bad/absent value returns None so the caller uses its default."""
    if not value:
        return None
    try:
        secs = int(value.strip())
    except (TypeError, ValueError):
        return None
    return secs if secs > 0 else None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _rate_limited_until(repo: "Repository") -> Optional[datetime]:
    """The active 429 back-off expiry (ADR-049), or None if not limited / expired.
    Stored in ``setting['tiingo_rate_limited_until']`` as an ISO datetime."""
    raw = repo.get_setting("tiingo_rate_limited_until") or ""
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts if _now_utc() < ts else None


def _record_rate_limit(
    repo: "Repository", retry_after_seconds: Optional[int] = None,
) -> str:
    """Persist a 429 back-off window (ADR-049) and return a human message naming
    the local clear time. Uses ``Retry-After`` when Tiingo gave one, else the
    1-hour default."""
    secs = retry_after_seconds or RATE_LIMIT_BACKOFF_SECONDS
    until = _now_utc() + timedelta(seconds=secs)
    repo.set_setting(
        "tiingo_rate_limited_until", until.isoformat(timespec="seconds"),
    )
    return (
        "Tiingo rate limit hit; pausing price fetches until "
        f"{until.astimezone().strftime('%H:%M')} local."
    )


def _backoff_message(until: datetime) -> str:
    return (
        "Skipped: Tiingo rate-limited until "
        f"{until.astimezone().strftime('%H:%M')} local."
    )


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
    until = _rate_limited_until(repo)
    if until is not None:
        return RefreshResult(
            fetched_at=None, new_prices_count=0, errors=[_backoff_message(until)],
        )
    last = repo.get_setting("tiingo_last_refresh_at") or ""
    if not force:
        hrs = _hours_since(last)
        if hrs is not None and hrs < LAUNCH_REFRESH_INTERVAL_HOURS:
            return RefreshResult(fetched_at=last, new_prices_count=0, errors=[])

    # Only securities actually held (≥1 txn) and not inside the give-up cooldown
    # — skip orphan securities from un-migrated accounts + uncovered tickers
    # (ADR-049).
    securities = repo.securities_to_price(
        cooldown_days=PRICE_UNAVAILABLE_COOLDOWN_DAYS,
    )
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
    except RateLimitedError as e:
        return RefreshResult(
            fetched_at=last or None, new_prices_count=0,
            errors=[_record_rate_limit(repo, e.retry_after_seconds)],
        )
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


def backfill_missing_history_into(
    repo: "Repository", *, min_points: int = 2, on_progress=None,
) -> RefreshResult:
    """Fetch full daily history for ONLY the tickered securities that don't yet
    have it (ADR-047) — the launch-time auto-backfill.

    ``Repository.securities_missing_history`` returns tickered securities with
    fewer than ``min_points`` stored prices (a lone "latest" close counts as
    missing). One Tiingo call per such security. This is naturally
    self-limiting: once a security is backfilled it drops out of the list, so a
    daily launch doesn't re-fetch securities that already have history — only
    newly tickered or freshly imported ones. No-op when the key is unset or
    nothing is missing; silent-friendly so the launch path can ignore failures.
    """
    api_key = repo.get_setting("tiingo_api_key")
    if not api_key:
        return RefreshResult(
            fetched_at=None, new_prices_count=0,
            errors=["No Tiingo API key set."],
        )
    until = _rate_limited_until(repo)
    if until is not None:
        return RefreshResult(
            fetched_at=None, new_prices_count=0, errors=[_backoff_message(until)],
        )
    # securities_missing_history already excludes orphan (no-txn) securities and
    # ones inside the give-up cooldown (ADR-049).
    securities = repo.securities_missing_history(
        min_points=min_points, cooldown_days=PRICE_UNAVAILABLE_COOLDOWN_DAYS,
    )
    if not securities:
        return RefreshResult(
            fetched_at=repo.get_setting("tiingo_last_refresh_at") or None,
            new_prices_count=0, errors=[],
        )

    client = TiingoClient(api_key)
    errors: list[str] = []
    count = 0
    total = len(securities)
    for i, sec in enumerate(securities):
        try:
            series = client.fetch_historical(sec.symbol)
            rows = [
                (sec.id, on_date, price, "tiingo") for on_date, price in series
            ]
            if rows:
                repo.bulk_upsert_security_prices(rows)
                repo.clear_security_price_unavailable(sec.id)
                count += len(rows)
            else:
                # Successful call, empty series → Tiingo doesn't cover this
                # ticker. Give up for the cooldown so it isn't re-fetched every
                # launch (ADR-049).
                repo.mark_security_price_unavailable(
                    sec.id, when=_now_utc().isoformat(timespec="seconds"),
                )
                errors.append(f"{sec.symbol}: no price history available")
        except RateLimitedError as e:
            errors.append(_record_rate_limit(repo, e.retry_after_seconds))
            break
        except SymbolNotFoundError as e:
            repo.mark_security_price_unavailable(
                sec.id, when=_now_utc().isoformat(timespec="seconds"),
            )
            errors.append(str(e))
        except PriceFetchError as e:  # transient (network) — retry next launch
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


def lookup_symbol_name(repo: "Repository", symbol: str) -> Optional[str]:
    """Best-effort security name for a ticker via Tiingo metadata (ADR-048) —
    used to auto-fill the Security field from a typed symbol. Returns ``None``
    when there's no API key, the provider can't be reached, or the ticker is
    unknown, so the caller silently falls back to manual name entry."""
    key = repo.get_setting("tiingo_api_key")
    if not key:
        return None
    try:
        meta = TiingoClient(key).fetch_metadata(symbol)
    except Exception:  # noqa: BLE001 — best-effort; offline/unknown → manual
        return None
    if not meta:
        return None
    name = (meta.get("name") or "").strip()
    return name or None


def backfill_security_history_into(
    repo: "Repository", *, security_id: int, symbol: str,
    start_date: Optional[str] = None,
) -> RefreshResult:
    """Fetch full daily history for ONE security (ADR-047) — backs the Stock
    Record screen's "Fetch from Tiingo" button after the user sets/corrects a
    ticker. Raises PriceFetchError when the key is missing or the security has
    no symbol; per-symbol fetch errors are collected, not raised."""
    api_key = repo.get_setting("tiingo_api_key")
    if not api_key:
        raise PriceFetchError(
            "No Tiingo API key set. Add one in Manage ▸ Securities first."
        )
    sym = (symbol or "").strip()
    if not sym:
        raise PriceFetchError("This security has no ticker symbol to fetch.")
    until = _rate_limited_until(repo)
    if until is not None:
        return RefreshResult(
            fetched_at=None, new_prices_count=0, errors=[_backoff_message(until)],
        )
    client = TiingoClient(api_key)
    errors: list[str] = []
    count = 0
    try:
        series = client.fetch_historical(sym, start_date)
        rows = [(security_id, on_date, price, "tiingo") for on_date, price in series]
        if rows:
            repo.bulk_upsert_security_prices(rows)
            repo.clear_security_price_unavailable(security_id)
            count = len(rows)
        else:
            repo.mark_security_price_unavailable(
                security_id, when=_now_utc().isoformat(timespec="seconds"),
            )
            errors.append(f"{sym}: no price history available")
    except RateLimitedError as e:
        errors.append(_record_rate_limit(repo, e.retry_after_seconds))
    except SymbolNotFoundError as e:
        repo.mark_security_price_unavailable(
            security_id, when=_now_utc().isoformat(timespec="seconds"),
        )
        errors.append(str(e))
    except PriceFetchError as e:
        errors.append(str(e))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
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
    until = _rate_limited_until(repo)
    if until is not None:
        return RefreshResult(
            fetched_at=None, new_prices_count=0, errors=[_backoff_message(until)],
        )
    # Only held (≥1 txn), non-given-up tickers (ADR-049) — skip orphan
    # securities from un-migrated accounts.
    securities = repo.securities_to_price(
        cooldown_days=PRICE_UNAVAILABLE_COOLDOWN_DAYS,
    )
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
                repo.clear_security_price_unavailable(sec.id)
                count += len(rows)
            else:
                repo.mark_security_price_unavailable(
                    sec.id, when=_now_utc().isoformat(timespec="seconds"),
                )
                errors.append(f"{sec.symbol}: no price history available")
        except RateLimitedError as e:
            errors.append(_record_rate_limit(repo, e.retry_after_seconds))
            break
        except SymbolNotFoundError as e:
            repo.mark_security_price_unavailable(
                sec.id, when=_now_utc().isoformat(timespec="seconds"),
            )
            errors.append(str(e))
        except PriceFetchError as e:  # transient (network) — retry next launch
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
