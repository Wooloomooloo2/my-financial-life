#!/usr/bin/env python3
"""Offline license signing tool (ADR-079) — NOT shipped with the app.

This is the *private* half of the licensing scheme. It holds (or is handed)
the Ed25519 **private key** and mints license keys the app verifies with the
matching public key in ``mfl_desktop/licensing.py``. It must never be bundled
into a release, and the private key must never be committed.

In production the Merchant-of-Record runs the equivalent of ``sign`` on
purchase; this script is the reference implementation + the dev signer.

Usage:
    # one-off: create a keypair. Prints the PUBLIC key to paste into
    # licensing.LICENSE_PUBLIC_KEY_B64; writes the PRIVATE key to a file.
    python tools/license_tool.py keygen --out tools/.dev_signing_key

    # mint a key for a buyer (edition = entitled major version)
    python tools/license_tool.py sign \
        --key tools/.dev_signing_key \
        --name "Ada Lovelace" --email ada@example.com --edition 1

    # sanity-check a minted key against the shipped public key
    python tools/license_tool.py verify --license "<key>"
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import date, timezone, datetime
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

# Import the app's verify-only side so the tool and the app can't drift on
# format. Run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mfl_desktop import licensing  # noqa: E402


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def cmd_keygen(args: argparse.Namespace) -> int:
    priv = Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )
    out = Path(args.out)
    out.write_text(base64.b64encode(priv_raw).decode() + "\n")
    out.chmod(0o600)
    print(f"Private key written to {out} (keep offline, never commit).")
    print("\nPaste this into mfl_desktop/licensing.LICENSE_PUBLIC_KEY_B64:\n")
    print(f'    LICENSE_PUBLIC_KEY_B64 = "{base64.b64encode(pub_raw).decode()}"')
    return 0


def _load_private(path: str) -> Ed25519PrivateKey:
    raw = base64.b64decode(Path(path).read_text().strip())
    return Ed25519PrivateKey.from_private_bytes(raw)


def cmd_sign(args: argparse.Namespace) -> int:
    priv = _load_private(args.key)
    issued = args.issued or datetime.now(timezone.utc).date().isoformat()
    payload = {
        "v": 1,
        "name": args.name,
        "email": args.email,
        "ed": int(args.edition),
        "iss": issued,
    }
    canonical = json.dumps(
        payload, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    payload_seg = _b64url(canonical)
    sig = priv.sign(payload_seg.encode("ascii"))
    key = f"{payload_seg}.{_b64url(sig)}"
    # Verify against our own public side before handing it out.
    info = licensing.parse_and_verify(
        key,
        public_key_b64=base64.b64encode(
            priv.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw,
            )
        ).decode(),
    )
    print(key)
    print(
        f"\n# {info.name} <{info.email}> — edition {info.edition}.x, "
        f"issued {info.issued}",
        file=sys.stderr,
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    try:
        info = licensing.parse_and_verify(args.license)
    except licensing.LicenseError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    status = licensing.evaluate(
        args.license, None, date.today(), licensing.__dict__.get("APP_EDITION", 1),
    )
    print(f"VALID — {info.name} <{info.email}>, edition {info.edition}.x, "
          f"issued {info.issued}")
    print(f"state={status.state} unlocked={status.unlocked}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="license_tool")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("keygen", help="Generate an Ed25519 signing keypair")
    p.add_argument("--out", default="tools/.dev_signing_key",
                   help="Where to write the private key")
    p.set_defaults(func=cmd_keygen)

    p = sub.add_parser("sign", help="Mint a license key")
    p.add_argument("--key", required=True, help="Private key file")
    p.add_argument("--name", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--edition", default="1", help="Entitled major version")
    p.add_argument("--issued", default=None, help="ISO issue date (default today)")
    p.set_defaults(func=cmd_sign)

    p = sub.add_parser("verify", help="Verify a key against the shipped public key")
    p.add_argument("--license", required=True)
    p.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
