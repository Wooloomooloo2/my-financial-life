"""openexchangerates.org client + refresh helpers (ADR-035).

Pure Python — no Qt imports, no threading; callers wrap the synchronous
``refresh_latest_into`` in a worker if they want it non-blocking. The CLI
``mfl_desktop.cli currencies refresh`` uses the synchronous path
directly.

Free-tier constraints:

- 1000 requests / month
- USD-base only (other bases are paid)
- Endpoints used:
  - ``/latest.json?app_id=KEY`` — today's rates
  - ``/historical/YYYY-MM-DD.json?app_id=KEY`` — one past day

Rate provenance: every row this module writes has ``source='openexchangerates'``.
The ``setting`` table holds the API key (``oxr_api_key``) and the
timestamp of the last successful refresh (``oxr_last_refresh_at``).
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Iterable, Optional

if TYPE_CHECKING:
    from mfl_desktop.db.repository import Repository

logger = logging.getLogger(__name__)


# Skip the launch refresh if the last one was less than this many hours
# ago. 24h matches "we only need today's close" personal-finance scope
# and bounds the monthly API budget for a single-user file at ~30 calls.
LAUNCH_REFRESH_INTERVAL_HOURS = 24

# Max calls a single Backfill Historical verb is allowed to make without
# explicit confirmation. The Currencies dialog surfaces the cost up-front
# when it exceeds this.
HISTORICAL_BACKFILL_SOFT_CAP = 100


_BASE_URL = "https://openexchangerates.org/api"


class FxFetchError(RuntimeError):
    """Raised when the provider call fails or returns an error payload.

    Distinct from ``ValueError`` so the Currencies dialog can show
    "couldn't reach openexchangerates" without conflating it with a
    user-typed bad input."""


@dataclass(frozen=True)
class RefreshResult:
    """Summary returned by ``refresh_latest_into`` and
    ``backfill_historical``. ``new_rates_count`` is how many ``fx_rate``
    rows were upserted; ``errors`` is a list of human-readable strings
    suitable for surfacing in a small dialog log box."""
    fetched_at: Optional[str]
    new_rates_count: int
    errors: list[str]


class OpenExchangeRatesClient:
    """Thin wrapper around urllib for the two endpoints we use."""

    def __init__(self, api_key: str, timeout_seconds: float = 15.0) -> None:
        self._api_key = (api_key or "").strip()
        self._timeout = timeout_seconds

    def fetch_latest(
        self, quotes: Optional[Iterable[str]] = None,
    ) -> dict[str, Decimal]:
        """Today's USD→quote rates. ``quotes`` filters down to the listed
        currencies (cheaper response when we only care about three or
        four pairs); ``None`` returns every currency the provider knows.

        Returns ``{quote_currency: rate_as_decimal}``. Same call shape as
        ``fetch_historical`` so the launch-refresh loop and the backfill
        loop can share logic."""
        return self._fetch_rates(path="/latest.json", quotes=quotes)

    def fetch_historical(
        self, on_date: str, quotes: Optional[Iterable[str]] = None,
    ) -> dict[str, Decimal]:
        """USD→quote rates on a past date (``YYYY-MM-DD``)."""
        return self._fetch_rates(
            path=f"/historical/{on_date}.json", quotes=quotes,
        )

    def _fetch_rates(
        self,
        *,
        path: str,
        quotes: Optional[Iterable[str]],
    ) -> dict[str, Decimal]:
        if not self._api_key:
            raise FxFetchError(
                "No openexchangerates API key set. Add one in "
                "Manage ▸ Currencies."
            )
        params: dict[str, str] = {"app_id": self._api_key}
        if quotes is not None:
            symbols = ",".join(sorted({q.strip().upper() for q in quotes if q.strip()}))
            if symbols:
                # `symbols` works on the free tier and reduces response
                # weight; not strictly required but cheaper.
                params["symbols"] = symbols
        url = f"{_BASE_URL}{path}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # Provider returns JSON error bodies on 4xx; pluck the message
            # if we can so the user sees "Invalid app_id" rather than 401.
            try:
                body = json.loads(e.read().decode("utf-8"))
                msg = body.get("description") or body.get("message") or str(e)
            except Exception:
                msg = str(e)
            raise FxFetchError(f"openexchangerates: {msg}") from e
        except urllib.error.URLError as e:
            raise FxFetchError(
                f"Could not reach openexchangerates: {e.reason}"
            ) from e
        if "rates" not in payload:
            raise FxFetchError(
                "openexchangerates response missing 'rates' field."
            )
        return {
            ccy: Decimal(str(rate))
            for ccy, rate in payload["rates"].items()
        }


def _hours_since(iso_ts: str) -> Optional[float]:
    """Hours between ``iso_ts`` and now (UTC). Returns ``None`` when the
    timestamp is empty or unparseable — caller treats that as "stale,
    needs refresh."""
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - ts
    return delta.total_seconds() / 3600.0


def refresh_latest_into(
    repo: "Repository", *, force: bool = False,
) -> RefreshResult:
    """Fetch today's USD→* rates and upsert them into ``fx_rate``.

    No-op when:

    - the API key is unset (``RefreshResult`` returns an explanatory error)
    - the last refresh was less than ``LAUNCH_REFRESH_INTERVAL_HOURS`` ago
      and ``force=False`` (the launch path uses this; the dialog's
      Refresh Now button passes ``force=True``)
    - no non-base accounts exist (nothing to fetch — single-currency users
      pay no API budget)

    Records the refresh timestamp in ``setting.oxr_last_refresh_at`` on
    success. Failures leave the timestamp untouched so the next launch
    retries.
    """
    api_key = repo.get_setting("oxr_api_key")
    if not api_key:
        return RefreshResult(
            fetched_at=None,
            new_rates_count=0,
            errors=["No openexchangerates API key set."],
        )
    last = repo.get_setting("oxr_last_refresh_at") or ""
    if not force:
        hrs = _hours_since(last)
        if hrs is not None and hrs < LAUNCH_REFRESH_INTERVAL_HOURS:
            return RefreshResult(
                fetched_at=last,
                new_rates_count=0,
                errors=[],
            )

    currencies = [
        ccy for ccy in repo.list_distinct_currencies() if ccy and ccy != "USD"
    ]
    base_currency = repo.get_setting("base_currency")
    if base_currency and base_currency != "USD" and base_currency not in currencies:
        currencies.append(base_currency)
    if not currencies:
        # Nothing to convert against — skip the API call entirely so a
        # single-currency user with the key set still pays nothing.
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        repo.set_setting("oxr_last_refresh_at", now)
        return RefreshResult(fetched_at=now, new_rates_count=0, errors=[])

    client = OpenExchangeRatesClient(api_key)
    today = datetime.now(timezone.utc).date().isoformat()
    errors: list[str] = []
    try:
        rates = client.fetch_latest(quotes=currencies)
    except FxFetchError as e:
        return RefreshResult(
            fetched_at=last or None,
            new_rates_count=0,
            errors=[str(e)],
        )
    count = 0
    for quote_ccy, rate in rates.items():
        try:
            repo.upsert_fx_rate(
                date=today, base="USD", quote=quote_ccy,
                rate=rate, source="openexchangerates",
            )
            count += 1
        except Exception as e:
            errors.append(f"{quote_ccy}: {e}")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    repo.set_setting("oxr_last_refresh_at", now)
    return RefreshResult(
        fetched_at=now, new_rates_count=count, errors=errors,
    )


# Sampling granularities for the historical backfill (ADR-065). One OXR
# `/historical` call is made per *sampled* date, so the granularity is the
# lever on API-call cost: monthly ≈ 12/yr, weekly ≈ 52/yr, daily ≈ 365/yr.
# The nearest-prior lookup (ADR-035) fills the gaps between sampled dates,
# so monthly is plenty for a personal-finance app.
BACKFILL_GRANULARITIES: tuple[str, ...] = ("monthly", "weekly", "daily")


def sample_backfill_dates(
    date_from: str, date_to: str, granularity: str = "monthly",
) -> list:
    """The dates a backfill would fetch over ``[date_from, date_to]``
    (inclusive) at the given sampling granularity — daily, weekly (every
    7 days from the start), or monthly (the start date then the 1st of
    each following month). ``date_to`` is always included as the final
    sample so the most recent edge gets a rate. Pure + side-effect-free
    so a dialog can call it for a live "≈ N API calls" estimate."""
    from datetime import date, timedelta

    if granularity not in BACKFILL_GRANULARITIES:
        raise ValueError(
            f"Unknown granularity {granularity!r}; expected one of "
            f"{BACKFILL_GRANULARITIES}"
        )
    d_from = date.fromisoformat(date_from)
    d_to = date.fromisoformat(date_to)
    if d_from > d_to:
        raise ValueError("date_from must be on or before date_to.")

    out: list = []
    if granularity == "daily":
        cur = d_from
        while cur <= d_to:
            out.append(cur)
            cur += timedelta(days=1)
    elif granularity == "weekly":
        cur = d_from
        while cur <= d_to:
            out.append(cur)
            cur += timedelta(days=7)
    else:  # monthly — start date, then the 1st of each subsequent month
        cur = d_from
        while cur <= d_to:
            out.append(cur)
            cur = (
                date(cur.year + 1, 1, 1) if cur.month == 12
                else date(cur.year, cur.month + 1, 1)
            )
    if not out or out[-1] != d_to:
        out.append(d_to)
    return out


def backfill_historical(
    repo: "Repository",
    *,
    quotes: Iterable[str],
    date_from: str,
    date_to: str,
    granularity: str = "monthly",
    on_progress=None,
) -> RefreshResult:
    """Fetch one historical-day response per *sampled* missing date in the
    range ``[date_from, date_to]`` (inclusive) and upsert the rates.

    ``granularity`` (``monthly`` / ``weekly`` / ``daily``, ADR-065) sets
    how many dates are sampled — and therefore the API-call cost. Skips
    sampled dates where every requested ``USD→quote`` already exists, so
    re-running the backfill is cheap. Each sampled date calls
    ``on_progress(index, total_samples)`` so the dialog can show a small
    progress bar.

    Raises ``FxFetchError`` if the API key is missing — callers should
    verify before invoking. Per-date errors are accumulated in
    ``RefreshResult.errors`` and the loop continues.
    """
    api_key = repo.get_setting("oxr_api_key")
    if not api_key:
        raise FxFetchError(
            "No openexchangerates API key set. Add one in "
            "Manage ▸ Currencies first."
        )
    quote_list = [q.strip().upper() for q in quotes if q.strip()]
    if not quote_list:
        return RefreshResult(fetched_at=None, new_rates_count=0, errors=[])

    sample_dates = sample_backfill_dates(date_from, date_to, granularity)

    client = OpenExchangeRatesClient(api_key)
    errors: list[str] = []
    count = 0
    total = len(sample_dates)
    for idx, cur_date in enumerate(sample_dates):
        iso = cur_date.isoformat()
        # Skip if every quote already has a row for this date.
        missing = [
            q for q in quote_list
            if repo.get_fx_rate_on(iso, "USD", q) is None
        ]
        if not missing:
            continue
        try:
            rates = client.fetch_historical(iso, quotes=missing)
            for q, r in rates.items():
                repo.upsert_fx_rate(
                    date=iso, base="USD", quote=q, rate=r,
                    source="openexchangerates",
                )
                count += 1
        except FxFetchError as e:
            errors.append(f"{iso}: {e}")
        if on_progress is not None:
            try:
                on_progress(idx + 1, total)
            except Exception:
                pass
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    repo.set_setting("oxr_last_refresh_at", now)
    return RefreshResult(
        fetched_at=now, new_rates_count=count, errors=errors,
    )
