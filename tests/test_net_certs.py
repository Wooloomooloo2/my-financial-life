"""TLS trust-store bootstrap for the frozen app (ADR-126).

Pins ``mfl_desktop.net_certs.ensure_ca_bundle`` — the one-line startup call in
``__main__.main`` that points OpenSSL at certifi's CA bundle so the frozen macOS
build can verify HTTPS certs (Tiingo prices, FX, bank feeds). Without it every
refresh in the ``.app`` failed with "unable to get local issuer certificate".

The contract this locks in:
  - sets ``SSL_CERT_FILE`` to ``certifi.where()`` when nothing else has,
  - never clobbers an existing ``SSL_CERT_FILE`` (user/admin override wins),
  - is a silent no-op when certifi is unavailable (bare dev run),
  - never raises (a trust bootstrap must not stop the app from launching).

Qt-free — runs on the base interpreter (``python3 tests/test_net_certs.py``) or
under pytest.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop import net_certs

_ENV = "SSL_CERT_FILE"


def _clear_env() -> None:
    os.environ.pop(_ENV, None)


def test_sets_certifi_bundle_when_unset():
    _clear_env()
    net_certs.ensure_ca_bundle()
    try:
        import certifi
    except ImportError:
        # No certifi in this env → must be a silent no-op, nothing set.
        assert _ENV not in os.environ
        return
    assert os.environ.get(_ENV) == certifi.where()
    assert os.path.isfile(os.environ[_ENV])


def test_existing_override_is_respected():
    sentinel = "/some/admin/override/cacert.pem"
    os.environ[_ENV] = sentinel
    try:
        net_certs.ensure_ca_bundle()
        assert os.environ[_ENV] == sentinel
    finally:
        _clear_env()


def test_idempotent():
    _clear_env()
    net_certs.ensure_ca_bundle()
    first = os.environ.get(_ENV)
    net_certs.ensure_ca_bundle()
    assert os.environ.get(_ENV) == first


def test_no_op_when_certifi_missing(monkeypatch=None):
    # Simulate certifi being unimportable: shadow it with a broken finder by
    # inserting None into sys.modules, which makes ``import certifi`` raise.
    _clear_env()
    saved = sys.modules.get("certifi")
    sys.modules["certifi"] = None  # type: ignore[assignment]
    try:
        net_certs.ensure_ca_bundle()  # must not raise
        assert _ENV not in os.environ  # nothing set from a missing bundle
    finally:
        if saved is not None:
            sys.modules["certifi"] = saved
        else:
            sys.modules.pop("certifi", None)
        _clear_env()


def test_never_raises_on_bad_bundle_path(monkeypatch=None):
    # If certifi.where() points at a nonexistent file, we must not set the env
    # var (and must not raise).
    _clear_env()

    class _FakeCertifi:
        @staticmethod
        def where():
            return "/definitely/not/here/cacert.pem"

    saved = sys.modules.get("certifi")
    sys.modules["certifi"] = _FakeCertifi()  # type: ignore[assignment]
    try:
        net_certs.ensure_ca_bundle()
        assert _ENV not in os.environ
    finally:
        if saved is not None:
            sys.modules["certifi"] = saved
        else:
            sys.modules.pop("certifi", None)
        _clear_env()


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
