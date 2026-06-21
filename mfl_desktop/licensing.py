"""Offline, locally-verified licensing (ADR-079).

MFL is a one-time-purchase, everything-included product with **no activation
server** (ADR-079 decision C1). A license is a small token **signed by our
private key** (held offline by the fulfilment side / Merchant-of-Record) and
**verified on-device against a shipped public key**. This module is the
verify-only half: it never holds or needs the private key, has no Qt or
network dependency, and is pure logic so it's trivially unit-testable.

It does three things:

1. **Parse + verify** a pasted license key (Ed25519 signature over the
   payload), returning the buyer + entitlement it encodes — or raising
   ``LicenseError`` for anything malformed, forged, or tampered.
2. **Entitlement check** — does the key's edition cover this build's major
   version? (A 1.x key unlocks all 1.x; 2.0 is a new key — ADR-079.)
3. **State machine** — combine "is there a valid key?" with "how far into the
   free trial are we?" into a single :class:`LicenseStatus` the UI renders.

Threat model is explicit in ADR-079: gentle friction for honest,
non-technical buyers, not a DRM fortress. Offline keys are crackable by the
determined; that's an accepted trade for staying local-first and backend-free.

## License key format (v1)

``<payload_b64url>.<signature_b64url>`` — two URL-safe-base64 segments
(unpadded) joined by a dot. The payload is canonical, compact JSON:

    {"v":1,"name":"Ada Lovelace","email":"ada@x.io","ed":1,"iss":"2026-06-21"}

The signature is Ed25519 over the **exact payload segment bytes** (the
base64 text, not the decoded JSON — so verification never depends on
re-serialising the JSON identically). ``ed`` is the entitled major version;
``iss`` is the issue date (informational). See ``tools/license_tool.py`` for
the offline signer.
"""
from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import date
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# ── Shipped public key ────────────────────────────────────────────────────
# The public half of the license-signing keypair. The matching PRIVATE key is
# held offline by the fulfilment side and MUST NEVER be committed or bundled
# (ADR-079). This default is a DEVELOPMENT key (its private half lives only in
# the gitignored tools/.dev_signing_key) — replace it with the production
# public key before shipping paid builds. Verification can also be pointed at
# any key via the ``public_key_b64`` argument (used by tests).
LICENSE_PUBLIC_KEY_B64 = "6fS7nETvW03i+9A2PFgOw0QKMkoNemzz/6l2me0JsDQ="

# Free, full-feature trial length before a key is required (ADR-079). Generous
# because the model is buy-once and the goal is conversion, not lockout.
TRIAL_DAYS = 30

_LICENSE_FORMAT_VERSION = 1


class LicenseError(Exception):
    """A license key is malformed, forged, tampered with, or otherwise
    unusable. The message is safe to show the user."""


@dataclass(frozen=True)
class LicenseInfo:
    """The verified contents of a license key. Only ever constructed after a
    good signature, so its fields can be trusted."""
    name: str
    email: str
    edition: int       # entitled major version (``ed`` in the payload)
    issued: str        # ISO issue date (``iss``); informational
    raw: str           # the original key string, for re-persisting verbatim


# License state the UI renders. Ordered loosely worst→best.
STATE_EXPIRED = "expired"        # trial elapsed, no valid key
STATE_TRIAL = "trial"            # within the free trial, no key yet
STATE_LICENSED = "licensed"      # a valid, edition-covering key is installed
STATE_INVALID = "invalid"        # a key is stored but no longer verifies
STATE_WRONG_EDITION = "wrong_edition"  # valid key, but for a different major


@dataclass(frozen=True)
class LicenseStatus:
    """The resolved licensing state for this launch.

    ``unlocked`` is the single boolean the app gates on (licensed *or* still in
    trial). ``info`` is present only when a valid key is installed.
    ``trial_days_left`` is meaningful in the trial/expired states.
    """
    state: str
    unlocked: bool
    trial_days_left: int = 0
    info: Optional[LicenseInfo] = None
    message: str = ""


def _b64url_decode(segment: str) -> bytes:
    """Decode an unpadded URL-safe base64 segment, restoring padding."""
    pad = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + pad)
    except (binascii.Error, ValueError) as exc:
        raise LicenseError("License key is not valid base64.") from exc


def _load_public_key(public_key_b64: str) -> Ed25519PublicKey:
    try:
        raw = base64.b64decode(public_key_b64)
        return Ed25519PublicKey.from_public_bytes(raw)
    except (binascii.Error, ValueError) as exc:
        raise LicenseError("The application's license key is misconfigured.") from exc


def parse_and_verify(
    key_str: str, *, public_key_b64: str = LICENSE_PUBLIC_KEY_B64,
) -> LicenseInfo:
    """Verify ``key_str`` and return its :class:`LicenseInfo`.

    Raises :class:`LicenseError` (with a user-safe message) if the key is
    empty, malformed, signed by the wrong key, tampered with, or carries an
    unexpected payload. Whitespace around / within the pasted key is tolerated
    so a copy-paste with stray newlines still works.
    """
    if not key_str or not key_str.strip():
        raise LicenseError("No license key entered.")
    cleaned = "".join(key_str.split())  # drop all whitespace incl. newlines
    parts = cleaned.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise LicenseError("License key is not in the expected format.")
    payload_seg, sig_seg = parts

    signature = _b64url_decode(sig_seg)
    public_key = _load_public_key(public_key_b64)
    try:
        # Sign/verify over the exact payload segment bytes (not re-serialised
        # JSON), so verification is independent of JSON formatting.
        public_key.verify(signature, payload_seg.encode("ascii"))
    except InvalidSignature as exc:
        raise LicenseError(
            "This license key is not valid (signature check failed)."
        ) from exc

    try:
        payload = json.loads(_b64url_decode(payload_seg))
    except (ValueError, UnicodeDecodeError) as exc:
        raise LicenseError("License key payload is unreadable.") from exc
    if not isinstance(payload, dict):
        raise LicenseError("License key payload is malformed.")
    if payload.get("v") != _LICENSE_FORMAT_VERSION:
        raise LicenseError(
            "This license key was issued in an unsupported format."
        )
    try:
        edition = int(payload["ed"])
    except (KeyError, TypeError, ValueError) as exc:
        raise LicenseError("License key is missing its edition.") from exc

    return LicenseInfo(
        name=str(payload.get("name", "")),
        email=str(payload.get("email", "")),
        edition=edition,
        issued=str(payload.get("iss", "")),
        raw=cleaned,
    )


def edition_covers(info: LicenseInfo, app_major: int) -> bool:
    """Does ``info``'s entitlement unlock an app of major version
    ``app_major``? A key entitles its edition and any **older** major (so a
    newer license still runs an older build); a 2.0 build needs a 2.x key
    (ADR-079)."""
    return app_major <= info.edition


def trial_days_left(start_iso: str, today: date, days: int = TRIAL_DAYS) -> int:
    """Whole days remaining in a trial that began ``start_iso`` (ISO date),
    as of ``today``. Clamped at 0; a bad/empty start date reads as expired."""
    try:
        start = date.fromisoformat(start_iso)
    except (ValueError, TypeError):
        return 0
    elapsed = (today - start).days
    if elapsed < 0:            # clock moved backwards / future start — be lenient
        return days
    return max(0, days - elapsed)


def evaluate(
    license_key: Optional[str],
    trial_start_iso: Optional[str],
    today: date,
    app_major: int,
    *,
    public_key_b64: str = LICENSE_PUBLIC_KEY_B64,
    trial_days: int = TRIAL_DAYS,
) -> LicenseStatus:
    """Resolve the launch's :class:`LicenseStatus` from the persisted key and
    trial-start date. Pure: callers inject ``today`` and the persisted values.

    Precedence: a valid, edition-covering key wins (LICENSED). A stored key
    that no longer verifies (INVALID) or covers the wrong major
    (WRONG_EDITION) falls through to the trial so the user isn't hard-locked
    by a bad key — but those states are reported so the UI can flag them.
    Otherwise it's TRIAL (within the window) or EXPIRED.
    """
    if license_key and license_key.strip():
        try:
            info = parse_and_verify(license_key, public_key_b64=public_key_b64)
        except LicenseError as exc:
            # Fall through to trial, but tell the UI the stored key is bad.
            return _trial_or_expired(
                trial_start_iso, today, trial_days,
                fallback_state=STATE_INVALID, fallback_msg=str(exc),
            )
        if not edition_covers(info, app_major):
            return _trial_or_expired(
                trial_start_iso, today, trial_days,
                fallback_state=STATE_WRONG_EDITION,
                fallback_msg=(
                    f"This license covers version {info.edition}.x, but this "
                    f"is version {app_major}.x. A {app_major}.0 upgrade key is "
                    f"required."
                ),
            )
        return LicenseStatus(
            state=STATE_LICENSED, unlocked=True, info=info,
            message=f"Licensed to {info.name}" if info.name else "Licensed",
        )
    return _trial_or_expired(trial_start_iso, today, trial_days)


def _trial_or_expired(
    trial_start_iso: Optional[str],
    today: date,
    trial_days: int,
    *,
    fallback_state: Optional[str] = None,
    fallback_msg: str = "",
) -> LicenseStatus:
    left = trial_days_left(trial_start_iso or "", today, trial_days)
    if left > 0:
        msg = fallback_msg or f"Trial — {left} day{'s' if left != 1 else ''} left"
        return LicenseStatus(
            state=fallback_state or STATE_TRIAL,
            unlocked=True, trial_days_left=left, message=msg,
        )
    return LicenseStatus(
        state=fallback_state or STATE_EXPIRED,
        unlocked=False, trial_days_left=0,
        message=fallback_msg or "Your free trial has ended.",
    )
