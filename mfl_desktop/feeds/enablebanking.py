"""Enable Banking client (ADR-077 Amendment 2) — free UK/EU Open Banking.

The practical GoCardless replacement: self-serve, free for connecting your own
accounts, and it covers UK banks (incl. HSBC) that no OFX feed reaches. Each
user registers their own free application at Enable Banking and supplies their
``application_id`` + RSA private key; nothing is centrally billed (ADR-035).

Auth differs from GoCardless — every request carries an **RS256-signed JWT**
(header ``{typ,alg,kid=application_id}``, claims ``iss=enablebanking.com``,
``aud=api.enablebanking.com``, ``iat``/``exp`` ≤ 24 h) signed with the
application's private key. That is the one new dependency (``cryptography``);
the JWT is assembled by hand (no PyJWT) to keep it to a single library.

Flow (see ADR-077 Amendment 2):
  aspsps   GET  /aspsps?country=GB
  auth     POST /auth      {aspsp, access, psu_type, redirect_url, state} -> {url, authorization_id}
  session  POST /sessions  {code}  -> {session_id, accounts:[{uid,...}]}
  data     GET  /accounts/{uid}/transactions?date_from&date_to[&continuation_key]

Consent is a browser round-trip like GoCardless: open ``url``, authenticate
with the bank, and the redirect carries ``?code=…`` which is exchanged for a
session. The ``opener`` and ``now_fn`` arguments are injectable so JWT assembly
and request construction are unit-testable without the network or a clock.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

BASE_URL = "https://api.enablebanking.com"

# Only booked transactions are imported; pending rows mutate before they post.
STATUS_BOOKED = "BOOK"


class EnableBankingError(RuntimeError):
    """An Enable Banking call failed — network, auth, or an error payload.

    Distinct from ``ValueError`` so the UI can say "couldn't reach Enable
    Banking / check your application key" without conflating it with bad input.
    """


@dataclass(frozen=True)
class Aspsp:
    """A bank ("Account Servicing Payment Service Provider")."""
    name: str
    country: str


@dataclass(frozen=True)
class AuthSession:
    url: str             # hosted bank-consent URL to open in the browser
    authorization_id: str


@dataclass(frozen=True)
class LinkedAccount:
    uid: str
    name: str = ""
    iban: str = ""
    currency: str = ""


@dataclass(frozen=True)
class SessionResult:
    session_id: str
    accounts: list[LinkedAccount] = field(default_factory=list)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def build_jwt(
    application_id: str,
    private_key_pem: bytes,
    *,
    ttl: int = 3600,
    now: Optional[float] = None,
) -> str:
    """Assemble + RS256-sign an Enable Banking API JWT.

    ``now`` is injectable so ``exp`` is deterministic in tests."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    issued = int(now if now is not None else time.time())
    header = {"typ": "JWT", "alg": "RS256", "kid": application_id}
    claims = {
        "iss": "enablebanking.com",
        "aud": "api.enablebanking.com",
        "iat": issued,
        "exp": issued + ttl,
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(claims, separators=(",", ":")).encode())
    )
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    signature = key.sign(
        signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256(),
    )
    return signing_input + "." + _b64url(signature)


class EnableBankingClient:
    def __init__(
        self,
        application_id: str,
        private_key_pem: bytes,
        *,
        base_url: str = BASE_URL,
        opener: Optional[Callable] = None,
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self._app_id = application_id
        self._key_pem = private_key_pem
        self._base = base_url.rstrip("/")
        self._opener = opener or urllib.request.urlopen
        self._now = now_fn or time.time
        self._jwt_cache: Optional[tuple[str, float]] = None  # (token, expiry)

    # ── transport ──

    def _jwt(self) -> str:
        now = self._now()
        if self._jwt_cache is not None and now < self._jwt_cache[1] - 60:
            return self._jwt_cache[0]
        ttl = 3600
        token = build_jwt(self._app_id, self._key_pem, ttl=ttl, now=now)
        self._jwt_cache = (token, now + ttl)
        return token

    def _request(self, method: str, path: str, *, body=None, query=None) -> dict:
        url = self._base + path
        if query:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in query.items() if v is not None}
            )
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._jwt()}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
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
            raise EnableBankingError(
                f"Enable Banking {method} {path} → HTTP {e.code}. {detail}".strip()
            )
        except urllib.error.URLError as e:
            raise EnableBankingError(f"Could not reach Enable Banking: {e.reason}")
        return json.loads(raw) if raw else {}

    # ── API ──

    def list_aspsps(self, country: str = "GB") -> list[Aspsp]:
        data = self._request("GET", "/aspsps", query={"country": country})
        out: list[Aspsp] = []
        for r in data.get("aspsps", []) or []:
            out.append(Aspsp(name=r.get("name", ""), country=r.get("country", country)))
        out.sort(key=lambda a: a.name.lower())
        return out

    def start_authorization(
        self,
        *,
        aspsp_name: str,
        country: str,
        redirect_url: str,
        state: str,
        valid_until: str,
        psu_type: str = "personal",
    ) -> AuthSession:
        """Begin bank consent. ``valid_until`` is an ISO-8601 UTC instant
        bounding the access window (Enable Banking caps it per bank, ~90-180d)."""
        data = self._request(
            "POST", "/auth",
            body={
                "aspsp": {"name": aspsp_name, "country": country},
                "access": {"valid_until": valid_until},
                "psu_type": psu_type,
                "redirect_url": redirect_url,
                "state": state,
            },
        )
        url, auth_id = data.get("url"), data.get("authorization_id")
        if not url or not auth_id:
            raise EnableBankingError("Enable Banking /auth response missing url/authorization_id.")
        return AuthSession(url=url, authorization_id=auth_id)

    def create_session(self, code: str) -> SessionResult:
        """Exchange the redirect ``code`` for a session + the linked accounts."""
        data = self._request("POST", "/sessions", body={"code": code})
        sid = data.get("session_id")
        if not sid:
            raise EnableBankingError("Enable Banking /sessions response missing session_id.")
        accounts = [
            LinkedAccount(
                uid=a.get("uid", ""),
                name=a.get("name", "") or (a.get("account_id", {}) or {}).get("iban", ""),
                iban=(a.get("account_id", {}) or {}).get("iban", ""),
                currency=a.get("currency", "") or "",
            )
            for a in (data.get("accounts", []) or [])
        ]
        return SessionResult(session_id=sid, accounts=accounts)

    def fetch_transactions(
        self, account_uid: str, *, date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> list[dict]:
        """All booked+pending transaction rows for an account, following the
        ``continuation_key`` pagination. Returns the raw provider dicts;
        ``normalize_enablebanking`` maps them to the import shape."""
        rows: list[dict] = []
        cont: Optional[str] = None
        for _ in range(50):  # hard page cap — guards against a server loop
            data = self._request(
                "GET", f"/accounts/{account_uid}/transactions",
                query={"date_from": date_from, "date_to": date_to,
                       "continuation_key": cont},
            )
            rows.extend(data.get("transactions", []) or [])
            cont = data.get("continuation_key")
            if not cont:
                break
        return rows
