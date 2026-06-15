"""GoCardless Bank Account Data client (ADR-077, Arc H round 1).

Free UK/EU Open Banking. Pure stdlib ``urllib`` — no Qt, no new dependency.
Each user supplies their own ``secret_id`` / ``secret_key`` (registered free
at GoCardless); these persist in the ``setting`` table, the short-lived access
token only in memory.

Flow (see ADR-077):
  token        POST /token/new/      {secret_id, secret_key} -> access (24h)
  institutions GET  /institutions/?country=GB
  consent      POST /requisitions/   {redirect, institution_id, reference} -> {id, link}
  poll         GET  /requisitions/{id}/   -> {status, accounts:[...]}  (LN == linked)
  data         GET  /accounts/{id}/transactions|balances|details/

The ``opener`` argument is injectable (defaults to ``urllib.request.urlopen``)
so request construction is unit-testable without the network.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

BASE_URL = "https://bankaccountdata.gocardless.com/api/v2"

# A requisition is usable once its status is LN ("linked").
STATUS_LINKED = "LN"


class GoCardlessError(RuntimeError):
    """A GoCardless call failed (network, auth, or an error payload).

    Distinct from ``ValueError`` so the UI can say "couldn't reach
    GoCardless / check your key" without conflating it with bad input."""


@dataclass(frozen=True)
class Institution:
    id: str
    name: str
    bic: str = ""
    transaction_total_days: int = 90


@dataclass(frozen=True)
class LinkSession:
    requisition_id: str
    link: str            # hosted bank-consent URL to open in the browser


@dataclass(frozen=True)
class LinkStatus:
    status: str
    account_ids: list[str] = field(default_factory=list)

    @property
    def linked(self) -> bool:
        return self.status == STATUS_LINKED


class GoCardlessClient:
    def __init__(
        self,
        secret_id: str,
        secret_key: str,
        *,
        base_url: str = BASE_URL,
        opener: Optional[Callable] = None,
    ) -> None:
        self._secret_id = secret_id
        self._secret_key = secret_key
        self._base = base_url.rstrip("/")
        self._opener = opener or urllib.request.urlopen
        self._access: Optional[str] = None

    # ── transport ──

    def _request(self, method: str, path: str, *, body=None, auth: bool = True) -> dict:
        url = self._base + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = f"Bearer {self._token()}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            raise GoCardlessError(f"GoCardless {method} {path} → HTTP {e.code}. {detail}".strip())
        except urllib.error.URLError as e:
            raise GoCardlessError(f"Could not reach GoCardless: {e.reason}")
        return json.loads(raw) if raw else {}

    def _token(self) -> str:
        if self._access is None:
            data = self._request(
                "POST", "/token/new/", auth=False,
                body={"secret_id": self._secret_id, "secret_key": self._secret_key},
            )
            access = data.get("access")
            if not access:
                raise GoCardlessError("GoCardless token response had no 'access'.")
            self._access = access
        return self._access

    # ── API ──

    def list_institutions(self, country: str = "GB") -> list[Institution]:
        rows = self._request("GET", f"/institutions/?country={country}")
        out: list[Institution] = []
        for r in rows if isinstance(rows, list) else []:
            out.append(Institution(
                id=r.get("id", ""), name=r.get("name", ""),
                bic=r.get("bic", "") or "",
                transaction_total_days=int(r.get("transaction_total_days") or 90),
            ))
        out.sort(key=lambda i: i.name.lower())
        return out

    def create_requisition(
        self, institution_id: str, redirect: str, reference: str,
    ) -> LinkSession:
        data = self._request(
            "POST", "/requisitions/",
            body={
                "redirect": redirect,
                "institution_id": institution_id,
                "reference": reference,
                "user_language": "EN",
            },
        )
        rid, link = data.get("id"), data.get("link")
        if not rid or not link:
            raise GoCardlessError("GoCardless requisition response missing id/link.")
        return LinkSession(requisition_id=rid, link=link)

    def requisition_status(self, requisition_id: str) -> LinkStatus:
        data = self._request("GET", f"/requisitions/{requisition_id}/")
        return LinkStatus(
            status=data.get("status", ""),
            account_ids=list(data.get("accounts", []) or []),
        )

    def account_details(self, account_id: str) -> dict:
        return self._request("GET", f"/accounts/{account_id}/details/").get("account", {})

    def account_balances(self, account_id: str) -> list[dict]:
        return self._request("GET", f"/accounts/{account_id}/balances/").get("balances", [])

    def fetch_transactions(self, account_id: str) -> dict:
        """Return the provider's ``{'booked': [...], 'pending': [...]}`` block."""
        data = self._request("GET", f"/accounts/{account_id}/transactions/")
        return data.get("transactions", {}) or {}
