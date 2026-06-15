"""Plaid client (ADR-077 Amendment 2) — broad US/Canada auto-feed.

The heaviest provider. The user brings their own Plaid credentials (free Trial
tier: real data, 10 Items, US/Canada; or pay-as-you-go) — nothing centrally
billed. Auth is simple (``client_id`` + ``secret`` go in every request body,
not a header), but the connect step needs **Plaid Link** (a hosted bank-login
widget), and transactions use the incremental **/transactions/sync** cursor
endpoint.

Flow (see https://plaid.com/docs):
  link      POST /link/token/create  -> link_token  (drives Plaid Link / Hosted Link)
  exchange  POST /item/public_token/exchange {public_token} -> {access_token, item_id}
  accounts  POST /accounts/get {access_token} -> {accounts:[{account_id,name,...}]}
  data      POST /transactions/sync {access_token, cursor} -> {added,modified,removed,next_cursor,has_more}

Persist the ``access_token`` (per Item) and the ``cursor`` (per Item) — sync is
incremental, so the next refresh only pulls what changed. Pure stdlib
``urllib``; the ``opener`` is injectable for offscreen tests.

**Sign convention:** Plaid amounts are POSITIVE when money leaves the account
(a debit) and NEGATIVE when money comes in — the opposite of OFX. The
normaliser (``normalize_plaid``) handles this.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

# Plaid environments. "development" was retired; the free Trial runs on
# production with a 10-Item cap.
ENV_URLS = {
    "production": "https://production.plaid.com",
    "sandbox": "https://sandbox.plaid.com",
}


class PlaidError(RuntimeError):
    """A Plaid call failed — network, auth, or an error payload. Carries the
    Plaid ``error_code`` when present so callers (e.g. re-auth on
    ITEM_LOGIN_REQUIRED) can branch."""

    def __init__(self, message: str, error_code: str = "") -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class PlaidAccount:
    account_id: str
    name: str = ""
    mask: str = ""
    type: str = ""
    subtype: str = ""


@dataclass(frozen=True)
class SyncResult:
    """Incremental sync output. ``upserts`` is added + modified rows;
    ``removed_ids`` are transaction_ids Plaid dropped; ``cursor`` is the next
    cursor to persist for the following sync."""
    upserts: list = field(default_factory=list)
    removed_ids: list = field(default_factory=list)
    cursor: str = ""


class PlaidClient:
    def __init__(
        self,
        client_id: str,
        secret: str,
        *,
        environment: str = "production",
        base_url: Optional[str] = None,
        opener: Optional[Callable] = None,
    ) -> None:
        self._client_id = client_id
        self._secret = secret
        self._base = (base_url or ENV_URLS.get(environment, ENV_URLS["production"])).rstrip("/")
        self._opener = opener or urllib.request.urlopen

    # ── transport ──

    def _request(self, path: str, body: dict) -> dict:
        # client_id + secret authenticate every Plaid request via the body.
        payload = {"client_id": self._client_id, "secret": self._secret, **body}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._base + path, data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with self._opener(req, timeout=60) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            code, message = "", ""
            try:
                err = json.loads(e.read() or b"{}")
                code = err.get("error_code", "") or ""
                message = err.get("error_message", "") or ""
            except Exception:
                pass
            raise PlaidError(
                f"Plaid {path} → HTTP {e.code}. {code} {message}".strip(), code,
            )
        except urllib.error.URLError as e:
            raise PlaidError(f"Could not reach Plaid: {e.reason}")

    # ── API ──

    def list_institutions(self, *, country_codes=("US",), count: int = 5) -> list[dict]:
        """Probe-only: proves client_id/secret + connectivity without an Item."""
        data = self._request("/institutions/get", {
            "count": count, "offset": 0, "country_codes": list(country_codes),
        })
        return data.get("institutions", []) or []

    def create_link_token(
        self, *, user_id: str, client_name: str,
        products=("transactions",), country_codes=("US",), language: str = "en",
        redirect_uri: Optional[str] = None, hosted_link: Optional[dict] = None,
    ) -> str:
        body: dict = {
            "user": {"client_user_id": user_id},
            "client_name": client_name,
            "products": list(products),
            "country_codes": list(country_codes),
            "language": language,
        }
        if redirect_uri:
            body["redirect_uri"] = redirect_uri
        if hosted_link is not None:
            body["hosted_link"] = hosted_link
        data = self._request("/link/token/create", body)
        token = data.get("link_token")
        if not token:
            raise PlaidError("Plaid /link/token/create returned no link_token.")
        return token

    def create_hosted_link_token(
        self, *, user_id: str, client_name: str,
        products=("transactions",), country_codes=("US",), language: str = "en",
    ) -> tuple[str, str]:
        """Create a link token configured for **Hosted Link** (Plaid hosts the
        bank-login page — no embedded JS, no local web server). Returns
        ``(link_token, hosted_link_url)``; open the URL in the browser."""
        data = self._request("/link/token/create", {
            "user": {"client_user_id": user_id},
            "client_name": client_name,
            "products": list(products),
            "country_codes": list(country_codes),
            "language": language,
            "hosted_link": {},
        })
        token, url = data.get("link_token"), data.get("hosted_link_url")
        if not token or not url:
            raise PlaidError("Plaid hosted-link create returned no link_token/url.")
        return token, url

    def get_link_public_token(self, link_token: str) -> str:
        """Poll a (hosted) link session for its completed ``public_token``.

        Returns "" while the user has not finished in the browser yet."""
        data = self._request("/link/token/get", {"link_token": link_token})
        for session in data.get("link_sessions", []) or []:
            results = session.get("results") or {}
            for r in results.get("item_add_results", []) or []:
                if r.get("public_token"):
                    return r["public_token"]
            # Some responses surface it directly on the session.
            if session.get("public_token"):
                return session["public_token"]
        return ""

    def exchange_public_token(self, public_token: str) -> tuple[str, str]:
        """public_token (from Plaid Link) → (access_token, item_id). Persist
        the access_token."""
        data = self._request(
            "/item/public_token/exchange", {"public_token": public_token},
        )
        access, item = data.get("access_token"), data.get("item_id")
        if not access:
            raise PlaidError("Plaid token exchange returned no access_token.")
        return access, item or ""

    def accounts_get(self, access_token: str) -> list[PlaidAccount]:
        data = self._request("/accounts/get", {"access_token": access_token})
        return [
            PlaidAccount(
                account_id=a.get("account_id", ""), name=a.get("name", "") or "",
                mask=a.get("mask", "") or "", type=str(a.get("type", "") or ""),
                subtype=str(a.get("subtype", "") or ""),
            )
            for a in (data.get("accounts", []) or [])
        ]

    def sync_transactions(
        self, access_token: str, *, cursor: Optional[str] = None,
    ) -> SyncResult:
        """Pull everything new since ``cursor`` (None = full history), following
        ``has_more``. Returns added+modified rows, removed ids, and the next
        cursor to persist."""
        upserts: list[dict] = []
        removed: list[str] = []
        cur = cursor or ""
        for _ in range(100):  # page cap — guards against a server loop
            data = self._request("/transactions/sync", {
                "access_token": access_token, "cursor": cur,
            })
            upserts.extend(data.get("added", []) or [])
            upserts.extend(data.get("modified", []) or [])
            removed.extend(
                r.get("transaction_id", "")
                for r in (data.get("removed", []) or [])
            )
            cur = data.get("next_cursor", cur) or cur
            if not data.get("has_more"):
                break
        return SyncResult(upserts=upserts, removed_ids=removed, cursor=cur)
