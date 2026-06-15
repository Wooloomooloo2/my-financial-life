"""SimpleFIN Bridge client (ADR-077 Amendment 2) — ~$15/yr US auto-feed.

The simplest provider on the framework. The user pays SimpleFIN directly
(bring-your-own-credentials, nothing centrally billed) and gets a one-time
**setup token**; the app claims it once for a durable **access URL** (which
carries HTTP basic-auth creds in it) and thereafter a single
``GET {access_url}/accounts`` returns every account *with its transactions
inline* — no per-account calls, no OAuth, no signing.

Flow (see https://www.simplefin.org/protocol.html):
  claim   setup token is base64 of a one-time claim URL → POST it → access URL
  data    GET {access_url}/accounts?start-date=&end-date=  -> {errors, accounts:[{id,name,transactions:[...]}]}

Pure stdlib ``urllib`` (the access URL's embedded credentials are sent as a
Basic auth header). The ``opener`` argument is injectable so claiming and
request construction are unit-testable without the network.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional


class SimpleFinError(RuntimeError):
    """A SimpleFIN call failed — network, auth, claim, or an error payload."""


@dataclass(frozen=True)
class SimpleFinAccount:
    id: str
    name: str
    currency: str = ""
    balance: str = ""
    transactions: list = field(default_factory=list)  # raw provider txn dicts


def claim_access_url(setup_token: str, *, opener: Optional[Callable] = None) -> str:
    """Exchange a one-time setup token for the durable access URL.

    The setup token is base64 of a claim URL; POSTing to it (once) returns the
    access URL — store that, not the token (the token is single-use)."""
    opener = opener or urllib.request.urlopen
    try:
        claim_url = base64.b64decode(setup_token.strip()).decode("utf-8").strip()
    except Exception:
        raise SimpleFinError("That doesn't look like a SimpleFIN setup token.")
    if not claim_url.lower().startswith("http"):
        raise SimpleFinError("Setup token did not decode to a claim URL.")
    req = urllib.request.Request(claim_url, data=b"", method="POST")
    try:
        with opener(req, timeout=30) as resp:
            access_url = resp.read().decode("utf-8").strip()
    except urllib.error.HTTPError as e:
        raise SimpleFinError(f"SimpleFIN claim failed → HTTP {e.code}.")
    except urllib.error.URLError as e:
        raise SimpleFinError(f"Could not reach SimpleFIN: {e.reason}")
    if not access_url.lower().startswith("http"):
        raise SimpleFinError("SimpleFIN claim did not return an access URL.")
    return access_url


class SimpleFinClient:
    def __init__(self, access_url: str, *, opener: Optional[Callable] = None) -> None:
        # The access URL embeds basic-auth creds: https://user:pass@host/path.
        # Split them off so they ride in an Authorization header, not the URL.
        parts = urllib.parse.urlsplit(access_url)
        self._userinfo = ""
        host = parts.netloc
        if "@" in parts.netloc:
            self._userinfo, host = parts.netloc.rsplit("@", 1)
        self._base = urllib.parse.urlunsplit(
            (parts.scheme, host, parts.path.rstrip("/"), "", "")
        )
        self._opener = opener or urllib.request.urlopen

    def _request(self, path: str, *, query=None) -> dict:
        url = self._base + path
        if query:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in query.items() if v is not None}
            )
        headers = {"Accept": "application/json"}
        if self._userinfo:
            token = base64.b64encode(self._userinfo.encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self._opener(req, timeout=60) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise SimpleFinError(f"SimpleFIN GET {path} → HTTP {e.code}.")
        except urllib.error.URLError as e:
            raise SimpleFinError(f"Could not reach SimpleFIN: {e.reason}")
        return json.loads(raw) if raw else {}

    def list_accounts(
        self, *, start_date: Optional[int] = None, end_date: Optional[int] = None,
    ) -> list[SimpleFinAccount]:
        """All accounts with their transactions inline. ``start_date`` /
        ``end_date`` are unix timestamps (seconds)."""
        data = self._request(
            "/accounts", query={"start-date": start_date, "end-date": end_date},
        )
        errors = data.get("errors") or []
        if errors and not data.get("accounts"):
            raise SimpleFinError("SimpleFIN: " + "; ".join(str(e) for e in errors))
        out: list[SimpleFinAccount] = []
        for a in data.get("accounts", []) or []:
            out.append(SimpleFinAccount(
                id=a.get("id", ""),
                name=a.get("name", "") or (a.get("org", {}) or {}).get("name", ""),
                currency=a.get("currency", "") or "",
                balance=str(a.get("balance", "") or ""),
                transactions=a.get("transactions", []) or [],
            ))
        return out

    def fetch_transactions(
        self, account_id: str, *, start_date: Optional[int] = None,
        end_date: Optional[int] = None,
    ) -> list[dict]:
        """Raw provider transaction dicts for one account (filtered from the
        single /accounts response). ``normalize_simplefin`` maps them."""
        for acct in self.list_accounts(start_date=start_date, end_date=end_date):
            if acct.id == account_id:
                return acct.transactions
        return []
