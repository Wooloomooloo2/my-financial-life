# tools/ — developer/operator utilities (not shipped)

Scripts here are **not part of the application** and are never bundled into a
release build.

## `license_tool.py` — license signing (ADR-079)

The **private** half of the offline licensing scheme. It mints the license
keys the app verifies on-device with the public key baked into
`mfl_desktop/licensing.LICENSE_PUBLIC_KEY_B64`.

### Key custody (read this)

- The **private signing key must never be committed or bundled.** It is the
  only secret in the whole scheme; anyone who has it can forge licenses.
- The dev key lives at `tools/.dev_signing_key`, which is **gitignored**
  (`tools/.dev_signing_key` + `*.signing_key`). Keep it local.
- For real paid builds, generate a fresh **production** keypair, store the
  private key in a password manager / HSM offline, and paste the printed
  public key into `licensing.LICENSE_PUBLIC_KEY_B64`. The current shipped
  value is a **development** key — replace it before selling.
- In production the Merchant-of-Record (Paddle / Lemon Squeezy / FastSpring,
  per ADR-079) runs the equivalent signing step on purchase; this script is
  the reference implementation and the dev/manual signer.

### Usage

```sh
# one-off: create a keypair (prints the PUBLIC key, writes the PRIVATE key)
python tools/license_tool.py keygen --out tools/.dev_signing_key

# mint a key for a buyer (edition = entitled major version)
python tools/license_tool.py sign --key tools/.dev_signing_key \
    --name "Ada Lovelace" --email ada@example.com --edition 1

# verify a minted key against the shipped public key
python tools/license_tool.py verify --license "<key>"
```

The app-side equivalent of `verify` is `python -m mfl_desktop.cli license-check
--license "<key>"`.
