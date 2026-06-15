"""Provider-agnostic feed fetch/sync + credential storage (ADR-077 Amendment 2).

Qt-free so the UI stays thin and this stays offscreen-testable. Two concerns:

1. **Credential storage** — connection-level secrets live in the ``setting``
   table (ADR-035), keyed per provider; per-account/per-item state (OFX config,
   Enable Banking consent window, Plaid access-token + cursor) alongside.

2. **`fetch_raw_for_feed(repo, feed)`** — given a ``feed_account`` row, build
   the right client, pull recent transactions, and return them as the import
   pipeline's raw-txn dicts. The UI then runs those through the unchanged
   ``stage_feed`` → dedup → commit. Plaid's incremental cursor is advanced and
   persisted here.

The provider client classes are referenced via module attributes so tests can
substitute stubs without a network.
"""
from __future__ import annotations

import datetime
import json
from typing import Optional

from mfl_desktop.feeds import normalize, ofx_store
from mfl_desktop.feeds.enablebanking import EnableBankingClient
from mfl_desktop.feeds.plaid import PlaidClient
from mfl_desktop.feeds.simplefin import SimpleFinClient

# Provider keys (also used as feed_account.provider). ofx_direct lives in
# ofx_store.PROVIDER; kept together here for the UI's provider list.
OFX = ofx_store.PROVIDER          # "ofx_direct"
SIMPLEFIN = "simplefin"
ENABLEBANKING = "enablebanking"
PLAID = "plaid"

PROVIDER_LABELS = {
    OFX: "OFX Direct Connect (US banks)",
    ENABLEBANKING: "Enable Banking (UK/EU, incl. HSBC)",
    SIMPLEFIN: "SimpleFIN Bridge (US)",
    PLAID: "Plaid (US/Canada)",
}

_HISTORY_DAYS = 90


# ── setting keys ──
_SF_ACCESS_URL = "simplefin_access_url"
_EB_APP_ID = "enablebanking_app_id"
_EB_KEY = "enablebanking_private_key"
_EB_REDIRECT = "enablebanking_redirect_url"
_EB_FEED = "enablebanking_feed:{}"        # per MFL account_id → {uid, valid_until}
_PLAID_CLIENT_ID = "plaid_client_id"
_PLAID_SECRET = "plaid_secret"
_PLAID_ENV = "plaid_env"
_PLAID_ITEM = "plaid_item:{}"             # per item_id → {access_token, cursor}

# Default redirect target for Enable Banking consent. Must be whitelisted in
# the user's Enable Banking application; the desktop has no web server, so the
# user copies the redirected URL back (the page itself need not exist).
DEFAULT_EB_REDIRECT = "https://enablebanking.com/"


# ── SimpleFIN credentials ──

def get_simplefin_access_url(repo) -> Optional[str]:
    return repo.get_setting(_SF_ACCESS_URL)


def set_simplefin_access_url(repo, access_url: str) -> None:
    repo.set_setting(_SF_ACCESS_URL, access_url)


# ── Enable Banking credentials + per-feed consent ──

def get_enablebanking_app(repo) -> Optional[tuple[str, bytes]]:
    app_id = repo.get_setting(_EB_APP_ID)
    key = repo.get_setting(_EB_KEY)
    if not app_id or not key:
        return None
    return app_id, key.encode("utf-8")


def set_enablebanking_app(repo, app_id: str, private_key_pem: str) -> None:
    repo.set_setting(_EB_APP_ID, app_id)
    repo.set_setting(_EB_KEY, private_key_pem)


def get_enablebanking_redirect(repo) -> str:
    return repo.get_setting(_EB_REDIRECT) or DEFAULT_EB_REDIRECT


def set_enablebanking_redirect(repo, url: str) -> None:
    repo.set_setting(_EB_REDIRECT, url)


def get_enablebanking_feed(repo, account_id: int) -> dict:
    raw = repo.get_setting(_EB_FEED.format(account_id))
    try:
        return json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return {}


def set_enablebanking_feed(repo, account_id: int, data: dict) -> None:
    repo.set_setting(_EB_FEED.format(account_id), json.dumps(data))


# ── Plaid credentials + per-item token/cursor ──

def get_plaid_creds(repo) -> Optional[tuple[str, str, str]]:
    cid = repo.get_setting(_PLAID_CLIENT_ID)
    sek = repo.get_setting(_PLAID_SECRET)
    if not cid or not sek:
        return None
    return cid, sek, (repo.get_setting(_PLAID_ENV) or "production")


def set_plaid_creds(repo, client_id: str, secret: str, env: str = "production") -> None:
    repo.set_setting(_PLAID_CLIENT_ID, client_id)
    repo.set_setting(_PLAID_SECRET, secret)
    repo.set_setting(_PLAID_ENV, env)


def get_plaid_item(repo, item_id: str) -> dict:
    raw = repo.get_setting(_PLAID_ITEM.format(item_id))
    try:
        return json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return {}


def set_plaid_item(repo, item_id: str, data: dict) -> None:
    repo.set_setting(_PLAID_ITEM.format(item_id), json.dumps(data))


# ── credential presence (does Add need to collect creds first?) ──

def has_credentials(repo, provider: str) -> bool:
    if provider == SIMPLEFIN:
        return bool(get_simplefin_access_url(repo))
    if provider == ENABLEBANKING:
        return get_enablebanking_app(repo) is not None
    if provider == PLAID:
        return get_plaid_creds(repo) is not None
    return True  # OFX carries its credentials per-account in the config blob


# ── fetch dispatch ──

def _date_window() -> tuple[str, str]:
    today = datetime.date.today()
    start = today - datetime.timedelta(days=_HISTORY_DAYS)
    return start.isoformat(), today.isoformat()


def fetch_raw_for_feed(repo, feed) -> list[dict]:
    """Pull recent transactions for one ``feed_account`` as raw-txn dicts.

    Raises the provider's own error type on failure (network/auth/expiry)."""
    provider = feed.provider

    if provider == OFX:
        cfg = ofx_store.load_config(repo, feed.account_id)
        if cfg is None:
            raise ValueError("No saved OFX configuration for this account.")
        return ofx_store.fetch_transactions(cfg, days=_HISTORY_DAYS)

    if provider == SIMPLEFIN:
        access = get_simplefin_access_url(repo)
        if not access:
            raise ValueError("No SimpleFIN access URL stored.")
        client = SimpleFinClient(access)
        raw = client.fetch_transactions(feed.external_account_id)
        return normalize.normalize_simplefin(raw)

    if provider == ENABLEBANKING:
        app = get_enablebanking_app(repo)
        if app is None:
            raise ValueError("No Enable Banking application configured.")
        client = EnableBankingClient(app[0], app[1])
        start, end = _date_window()
        raw = client.fetch_transactions(
            feed.external_account_id, date_from=start, date_to=end,
        )
        return normalize.normalize_enablebanking(raw)

    if provider == PLAID:
        creds = get_plaid_creds(repo)
        if creds is None:
            raise ValueError("No Plaid credentials configured.")
        item_id = feed.requisition_id or ""
        item = get_plaid_item(repo, item_id)
        access_token = item.get("access_token")
        if not access_token:
            raise ValueError("No Plaid access token stored for this item.")
        client = PlaidClient(creds[0], creds[1], environment=creds[2])
        result = client.sync_transactions(access_token, cursor=item.get("cursor") or None)
        # Persist the advanced cursor so the next sync is incremental.
        item["cursor"] = result.cursor
        set_plaid_item(repo, item_id, item)
        return normalize.normalize_plaid(result.upserts, account_id=feed.external_account_id)

    raise ValueError(f"Unknown feed provider {provider!r}.")
