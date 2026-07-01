"""TLS trust-store bootstrap for the frozen app (ADR-126).

Every outbound HTTPS call in the app goes through ``urllib.request.urlopen``
with the *default* SSL context: Tiingo security prices (``prices.py``), FX
rates (``fx.py``), and the bank-feed clients (``feeds/*.py``). The default
context verifies the server certificate against OpenSSL's built-in default CA
search paths.

Inside a PyInstaller bundle on macOS those paths do not resolve: they point at
the *build machine's* OpenSSL location, which does not exist inside the signed
``.app``, and macOS's OpenSSL does **not** fall back to the system Keychain.
The result is that every price/FX/feed refresh fails with::

    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
    unable to get local issuer certificate (_ssl.c:1010)

certifi's ``cacert.pem`` (Mozilla's CA bundle) is already carried inside the
bundle, so the fix is simply to point OpenSSL at it. Setting ``SSL_CERT_FILE``
*before the first HTTPS call* makes ``ssl.create_default_context`` — and hence
every ``urlopen`` in the app — load that bundle. One chokepoint covers prices,
FX, and all bank feeds, current and future.

Cross-platform / dev safety (ADR-050):
  - We only set ``SSL_CERT_FILE`` when it is **not already set**, so a user or
    admin override always wins, and we never fight an OS that already has a
    working trust store (e.g. Windows, where Python loads the system store).
  - We only set it when certifi is importable *and* its bundle exists on disk,
    so an unfrozen dev run without certifi is a silent no-op.
The call is therefore harmless everywhere and only *adds* trust where the
frozen macOS build had none.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_CERT_FILE_ENV = "SSL_CERT_FILE"


def ensure_ca_bundle() -> None:
    """Point OpenSSL at certifi's CA bundle if nothing else has set the trust
    store yet. Idempotent and exception-safe — call once at startup, before any
    network I/O. Never raises: a TLS bootstrap must not be able to stop the app
    from launching."""
    if os.environ.get(_CERT_FILE_ENV):
        # An explicit override (user, admin, or a prior call) is already in
        # place — respect it rather than clobbering it with certifi's bundle.
        return
    try:
        import certifi

        bundle = certifi.where()
    except Exception:  # certifi absent (bare dev run) or import failed
        logger.debug("certifi unavailable; leaving default SSL trust store")
        return

    if bundle and os.path.isfile(bundle):
        os.environ[_CERT_FILE_ENV] = bundle
        logger.debug("Set %s to certifi bundle: %s", _CERT_FILE_ENV, bundle)
    else:
        logger.debug("certifi bundle path missing on disk: %r", bundle)
