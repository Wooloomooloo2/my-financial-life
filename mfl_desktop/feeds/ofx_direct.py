"""OFX Direct Connect client (ADR-077, Arc H — free US auto-feed).

A second provider on the ADR-077 feed framework. Where GoCardless is Open
Banking for the UK/EU, OFX Direct Connect is the free, fully-local mechanism
that still covers many **US** banks: the app HTTP-POSTs a signed OFX request
to the bank's own OFX server with the user's online-banking credentials, and
the bank returns an OFX statement document — the *same* format the file
importer already reads. So the response goes straight through the existing
``ofx_parser`` → raw-txn dicts → ``ImportService.stage_feed`` → dedup / review
/ commit. Zero new parsing or dedup logic.

No third party, no cost, no data leaving the user's machine for an aggregator.
Coverage has thinned over the years (some big banks dropped Direct Connect or
gate it behind Quicken); look a bank's connection details up at ofxhome.com
and verify with the ``ofx-check`` CLI probe before wiring any UI.

The request/response protocol is handled by ``ofxtools.Client`` (already a
dependency via the OFX file parser) rather than hand-rolled SGML — it gets the
headers, signon, NEWFILEUID, and request grouping right. The ``client_factory``
argument is injectable so request construction and the parse/stage path are
unit-testable without the network (and ``fetch_ofx(dryrun=True)`` returns the
request body without sending it).
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Callable, Optional

# Many FIs reject an unknown client application; impersonating Quicken
# (the historical Direct Connect client) is the widely-used default. The
# owner can override per server if their bank expects something else.
DEFAULT_APP_ID = "QWIN"
DEFAULT_APP_VERSION = "2700"
DEFAULT_OFX_VERSION = 102

# OFX bank ACCTTYPE values (credit cards / investments use their own request
# message, so they are modelled separately below, not in this set).
BANK_ACCT_TYPES = ("CHECKING", "SAVINGS", "MONEYMRKT", "CREDITLINE", "CD")


class OfxDirectError(RuntimeError):
    """An OFX Direct Connect call failed — network, auth, or an FI-side error.

    Distinct from ``ValueError`` so the UI can say "couldn't reach your bank /
    check your details" without conflating it with bad input."""


@dataclass(frozen=True)
class OfxServer:
    """A bank's OFX Direct Connect endpoint + identity (from ofxhome.com).

    ``client_uid`` is required by some OFX 1.0.2+ banks and **must be stable
    across sessions** for those banks (it identifies the device) — the config
    layer persists one generated value per server. Empty means "don't send
    one"; ``ofxtools`` only emits CLIENTUID when it is set.
    """

    url: str
    org: str
    fid: str
    app_id: str = DEFAULT_APP_ID
    app_version: str = DEFAULT_APP_VERSION
    ofx_version: int = DEFAULT_OFX_VERSION
    client_uid: str = ""


@dataclass(frozen=True)
class OfxAccountSpec:
    """Which account to pull, and how to address it in the OFX request."""

    acct_id: str                  # the account number
    acct_type: str = "CHECKING"   # a BANK_ACCT_TYPES value, or CREDITCARD / INVESTMENT
    bank_id: str = ""             # routing/sort number — bank accounts only
    broker_id: str = ""           # broker id — investment accounts only

    @property
    def is_credit_card(self) -> bool:
        return self.acct_type.upper() in ("CREDITCARD", "CC")

    @property
    def is_investment(self) -> bool:
        return self.acct_type.upper() in ("INVESTMENT", "INV")


# ── OFX status extraction ──
# A failed signon (bad password, locked account) comes back as a valid OFX
# document whose STATUS carries the error, not as an HTTP error — so the parser
# would just see "no statements". Pull the first STATUS block out so the UI can
# show the bank's own message. Works for both 1.x SGML (often no close tags)
# and 2.x XML.
_CODE_RE = re.compile(r"<CODE>\s*([^<\s]+)", re.IGNORECASE)
_SEVERITY_RE = re.compile(r"<SEVERITY>\s*([^<\s]+)", re.IGNORECASE)
_MESSAGE_RE = re.compile(r"<MESSAGE>\s*([^<]*)", re.IGNORECASE)


def ofx_status(ofx_bytes: bytes) -> Optional[dict]:
    """The first OFX ``STATUS`` block as ``{code, severity, message}``, or None.

    Code ``"0"`` is success; anything else is an FI-side error worth surfacing.
    """
    try:
        text = ofx_bytes.decode("utf-8", "replace")
    except Exception:
        return None
    m = _CODE_RE.search(text)
    if not m:
        return None
    sev = _SEVERITY_RE.search(text)
    msg = _MESSAGE_RE.search(text)
    return {
        "code": m.group(1).strip(),
        "severity": (sev.group(1).strip() if sev else ""),
        "message": (msg.group(1).strip() if msg else ""),
    }


class OfxDirectClient:
    def __init__(
        self,
        server: OfxServer,
        username: str,
        password: str,
        *,
        client_factory: Optional[Callable[[OfxAccountSpec], object]] = None,
    ) -> None:
        self._server = server
        self._username = username
        self._password = password
        # Injectable so tests can supply a fake whose request_statements()
        # returns canned OFX bytes; defaults to a real ofxtools OFXClient.
        self._client_factory = client_factory or self._make_client

    def _make_client(self, spec: OfxAccountSpec):
        from ofxtools.Client import OFXClient

        return OFXClient(
            self._server.url,
            userid=self._username,
            clientuid=self._server.client_uid or None,
            org=self._server.org,
            fid=self._server.fid,
            version=self._server.ofx_version,
            appid=self._server.app_id,
            appver=self._server.app_version,
            bankid=spec.bank_id or None,
            brokerid=spec.broker_id or None,
        )

    def _build_request(self, spec: OfxAccountSpec, dtstart, dtend):
        from ofxtools.Client import CcStmtRq, InvStmtRq, StmtRq

        if spec.is_credit_card:
            return CcStmtRq(
                acctid=spec.acct_id, dtstart=dtstart, dtend=dtend, inctran=True,
            )
        if spec.is_investment:
            return InvStmtRq(
                acctid=spec.acct_id, dtstart=dtstart, dtend=dtend,
                inctran=True, incoo=False, incpos=True, incbal=True,
            )
        return StmtRq(
            acctid=spec.acct_id, accttype=spec.acct_type.upper(),
            dtstart=dtstart, dtend=dtend, inctran=True,
        )

    def fetch_ofx(
        self,
        spec: OfxAccountSpec,
        *,
        days: int = 90,
        dtstart: Optional[datetime.datetime] = None,
        dtend: Optional[datetime.datetime] = None,
        dryrun: bool = False,
        skip_profile: bool = True,
        timeout: float = 30.0,
    ) -> bytes:
        """POST a statement request and return the raw OFX response bytes.

        ``dryrun=True`` returns the *request* body without sending (for tests /
        the ``--raw`` probe). ``skip_profile=True`` (default) POSTs straight to
        the configured URL instead of first asking the bank for its service
        URLs — fewer round-trips and fewer banks that choke on the profile call.
        """
        dtend = dtend or datetime.datetime.now(datetime.timezone.utc)
        dtstart = dtstart or (dtend - datetime.timedelta(days=days))
        rq = self._build_request(spec, dtstart, dtend)
        client = self._client_factory(spec)
        try:
            resp = client.request_statements(
                self._password, rq,
                dryrun=dryrun, skip_profile=skip_profile, timeout=timeout,
            )
            return resp.read()
        except OfxDirectError:
            raise
        except Exception as e:  # network, HTTP, ofxtools assembly errors
            raise OfxDirectError(
                f"OFX Direct Connect to {self._server.url} failed: {e}"
            )

    def fetch_transactions(self, spec: OfxAccountSpec, *, days: int = 90, **kw) -> list[dict]:
        """Pull the account and return raw-txn dicts ready for ``stage_feed``.

        The OFX response is the same document the file importer reads, so it
        goes straight through ``ofx_parser.parse_ofx`` — no feed-specific
        normalisation. An FI-side error (e.g. bad password) is surfaced from the
        OFX STATUS rather than as an opaque "no statements" parse failure.
        """
        from mfl_desktop.import_engine.ofx_parser import parse_ofx

        ofx_bytes = self.fetch_ofx(spec, days=days, **kw)
        status = ofx_status(ofx_bytes)
        if status and status["code"] not in ("0", "", None):
            detail = status["message"] or "(no message)"
            raise OfxDirectError(
                f"Bank returned OFX status {status['code']} "
                f"{status['severity']}: {detail}"
            )
        try:
            return parse_ofx(ofx_bytes, filename="ofx-direct")
        except ValueError as e:
            raise OfxDirectError(f"Could not read the bank's OFX response: {e}")
