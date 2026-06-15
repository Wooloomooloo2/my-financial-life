"""Persistence + client-building glue for OFX Direct Connect feeds (ADR-077).

Qt-free so it can be unit-tested offscreen. The per-account connection config
(server URL/ORG/FID, app identity, credentials, and which account to pull)
lives as a JSON blob in the ``setting`` table under ``ofx_config:{account_id}``
— the same place GoCardless secrets and the OXR/Tiingo keys sit (ADR-035). The
``feed_account`` row is just the link marker (provider + external account id +
status + last-synced); the credentials never go in it.

A stable ``client_uid`` is generated once on first save and kept — some OFX
1.0.2+ banks tie it to the registered device and reject a changing value.
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from mfl_desktop.feeds.ofx_direct import OfxAccountSpec, OfxDirectClient, OfxServer

PROVIDER = "ofx_direct"
_KEY = "ofx_config:{}"

# Account types offered in the UI: OFX bank types plus the two that use their
# own request message (credit card / investment). (label, value) pairs.
ACCT_TYPE_CHOICES = (
    ("Checking", "CHECKING"),
    ("Savings", "SAVINGS"),
    ("Money market", "MONEYMRKT"),
    ("Line of credit", "CREDITLINE"),
    ("Certificate of deposit", "CD"),
    ("Credit card", "CREDITCARD"),
    ("Investment", "INVESTMENT"),
)

# Config keys that carry secrets — used when redacting for display/logging.
SECRET_FIELDS = ("password",)

_DEFAULTS = {
    "institution_name": "",
    "url": "",
    "org": "",
    "fid": "",
    "app_id": "QWIN",
    "app_version": "2700",
    "ofx_version": 102,
    "client_uid": "",
    "username": "",
    "password": "",
    "acct_id": "",
    "acct_type": "CHECKING",
    "bank_id": "",
    "broker_id": "",
}


def empty_config() -> dict:
    """A fresh config dict with the sensible defaults filled in."""
    return dict(_DEFAULTS)


def load_config(repo, account_id: int) -> Optional[dict]:
    """The stored config for an account, or None if it has no OFX feed config.

    Missing keys are back-filled from the defaults so older/partial blobs stay
    usable as the schema grows."""
    raw = repo.get_setting(_KEY.format(account_id))
    if not raw:
        return None
    try:
        stored = json.loads(raw)
    except (ValueError, TypeError):
        return None
    cfg = empty_config()
    cfg.update({k: v for k, v in stored.items() if k in cfg})
    return cfg


def save_config(repo, account_id: int, cfg: dict) -> dict:
    """Persist a config, generating + keeping a stable ``client_uid`` if blank.

    Returns the saved config (with the generated uid) so the caller can reflect
    it back into the UI."""
    out = empty_config()
    out.update({k: v for k, v in cfg.items() if k in out})
    if not str(out.get("client_uid") or "").strip():
        out["client_uid"] = str(uuid.uuid4())
    repo.set_setting(_KEY.format(account_id), json.dumps(out))
    return out


def clear_config(repo, account_id: int) -> None:
    """Remove the stored config (called when a feed is unlinked)."""
    repo.set_setting(_KEY.format(account_id), "")


def build_client(cfg: dict) -> tuple[OfxDirectClient, OfxAccountSpec]:
    """Turn a config dict into a ready client + account spec."""
    server = OfxServer(
        url=str(cfg.get("url", "")).strip(),
        org=str(cfg.get("org", "")).strip(),
        fid=str(cfg.get("fid", "")).strip(),
        app_id=str(cfg.get("app_id") or "QWIN").strip(),
        app_version=str(cfg.get("app_version") or "2700").strip(),
        ofx_version=int(cfg.get("ofx_version") or 102),
        client_uid=str(cfg.get("client_uid") or "").strip(),
    )
    spec = OfxAccountSpec(
        acct_id=str(cfg.get("acct_id", "")).strip(),
        acct_type=str(cfg.get("acct_type") or "CHECKING").strip(),
        bank_id=str(cfg.get("bank_id") or "").strip(),
        broker_id=str(cfg.get("broker_id") or "").strip(),
    )
    client = OfxDirectClient(
        server, str(cfg.get("username", "")), str(cfg.get("password", "")),
    )
    return client, spec


def fetch_transactions(cfg: dict, *, days: int = 90) -> list[dict]:
    """Convenience: build a client from config and pull raw-txn dicts.

    Raises ``OfxDirectError`` on any failure (network / auth / FI error)."""
    client, spec = build_client(cfg)
    return client.fetch_transactions(spec, days=days)
