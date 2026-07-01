# ADR-126 — Frozen-app TLS trust store: point OpenSSL at certifi's CA bundle

**Date:** 2026-07-01
**Status:** Accepted
**Related:** ADR-044 (Tiingo security-price client — the first HTTPS caller). ADR-035 (FX rate refresh — the second). ADR-077 (OFX Direct Connect) and the bank-feed clients under `feeds/*.py` (the rest). ADR-104 (PyInstaller packaging scaffold — `mfl.spec`). ADR-125 (Mac App Store sandbox — the `network.client`-entitled build where this first bit). ADR-050 (cross-platform-first — the fix must be a no-op on Windows/dev).

## Context

Every outbound HTTPS call in the desktop app goes through `urllib.request.urlopen` with the **default** SSL context — there is no `requests`/`httpx`; the whole network surface is stdlib `urllib` (ADR-099). Call sites: `prices.py` (Tiingo prices), `fx.py` (FX rates), and the bank-feed clients (`feeds/simplefin.py`, `enablebanking.py`, `gocardless.py`, `plaid.py`). The default context verifies the server certificate against OpenSSL's built-in **default CA search paths**.

Running the **packaged macOS `.app`** (PyInstaller bundle, `~/Applications/My Financial Life.app`), a price refresh failed for every ticker with:

```
Could not reach Tiingo for DIVO: [SSL: CERTIFICATE_VERIFY_FAILED]
certificate verify failed: unable to get local issuer certificate (_ssl.c:1010)
```

**Root cause.** Inside a frozen bundle, OpenSSL's default CA paths do not resolve: they are compiled to the *build machine's* OpenSSL prefix, which does not exist inside the signed `.app`, and macOS's OpenSSL does **not** fall back to the system Keychain (unlike Python-on-Windows, which loads the OS certificate store — which is why Windows was unaffected). So the default context has **no CA to verify against** and every TLS handshake fails at verification. This is the canonical macOS "unable to get local issuer certificate" failure; the normal python.org installer papers over it with the *Install Certificates.command* post-install step, which a frozen app never runs.

certifi's `cacert.pem` (Mozilla's CA bundle) was in fact already carried inside the bundle (`Contents/Resources/certifi/cacert.pem`, pulled in transitively) — but nothing told OpenSSL to use it.

Verified empirically before fixing: pointing `SSL_CERT_FILE` at a nonexistent path reproduces the exact `_ssl.c:1010` error against `api.tiingo.com`; pointing it at `certifi.where()` makes the handshake succeed (server then answers at the HTTP layer). `SSL_CERT_FILE` alone is sufficient — `SSL_CERT_DIR` is not needed.

## Decision

At startup, **before any network I/O**, point OpenSSL at certifi's CA bundle by setting the `SSL_CERT_FILE` environment variable to `certifi.where()`. `ssl.create_default_context` (and hence every `urlopen` in the app) reads that variable when it builds the default context, so **one chokepoint fixes prices, FX, and all bank feeds** — current and future call sites alike — rather than threading an explicit `ssl.SSLContext` through six modules (two of which inject openers).

- **New module `mfl_desktop/net_certs.py`** with `ensure_ca_bundle()`: import-guarded on certifi, sets `SSL_CERT_FILE` **only when it is not already set** and certifi's bundle **exists on disk**; idempotent and exception-safe (a trust bootstrap must never be able to stop the app launching).
- **Called once** as the first statement of `__main__.main()`, before `QApplication` and before the background FX/price refresh runnables start.
- **certifi promoted to an explicit, pinned desktop dependency** (`requirements-desktop.txt`, `certifi==2025.10.5`) — it was only transitively present; the fix now depends on it, so it is declared. It is *data-only* (a CA bundle), not an API dependency.
- **Deterministically bundled** in `mfl.spec` via `collect_data_files("certifi")` + `hiddenimports=["certifi"]`, so the `cacert.pem` is guaranteed to ship and stays shipped.

**Why env var, not an explicit context.** The env-var approach is the standard frozen-app remedy, is a single process-wide chokepoint that covers every existing and future `urlopen`, and composes with the feed clients' injectable openers without touching them. Rejected: (a) building a shared `SSLContext` and passing it to each `urlopen` — six edit sites, easy to miss a new one, and the feed openers are swapped in tests; (b) `truststore` (inject the OS trust store) — a heavier dep whose macOS path re-introduces Keychain reliance we just saw fail under the frozen/sandbox build; (c) do nothing and rely on transitive certifi — the bundle already *had* the file and still failed, because nothing pointed OpenSSL at it.

**Override-safe & cross-platform (ADR-050).** We never clobber an existing `SSL_CERT_FILE`, so a user/admin/corporate override wins. On Windows and in unfrozen dev the default trust store already resolves, so the call still runs but is effectively invisible; if certifi is somehow absent it is a silent no-op. The change only ever *adds* trust where the frozen build had none.

## Consequences

- The packaged macOS app can verify TLS again: Tiingo price refresh, FX refresh, and bank-feed connections all work in the `.app` (and in the sandboxed MAS build from ADR-125, which carries the `network.client` entitlement).
- One new pinned dependency (certifi), data-only. The "stdlib-only network surface" property is unchanged — we still make no third-party *API* calls; certifi contributes a file, not code paths.
- New regression test `tests/test_net_certs.py` pins the contract (sets-from-certifi / respects-override / idempotent / no-op-when-missing / never-raises); Qt-free, green on the base interpreter and on CI without certifi.
- Future HTTPS call sites need no special handling — they inherit the trust store for free. If a new packaging target strips certifi, the app degrades to the prior (broken-on-frozen-macOS) behaviour rather than crashing; the bundling assertion in `mfl.spec` is the guard against that.
