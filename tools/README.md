# tools/ — developer/operator utilities (not shipped)

Scripts here are **not part of the application** and are never bundled into a
release build.

## `standard_life_pdf_to_qif.py` — pension statement → QIF (ADR-153)

Standard Life exports nothing machine-readable for a Group Stakeholder plan; the
only artefact is a PDF transaction statement. This converts one into an
investment QIF the app's importer already understands, plus a unit-price CSV.

Needs `pypdf` (`pip install pypdf`) — not an app dependency.

```sh
# convert; writes <account>.qif + <account>-prices.csv next to the PDF
python tools/standard_life_pdf_to_qif.py statement.pdf --account "Standard Life Pension"

# then: import the QIF through the app's normal Import flow, and load the prices
# (app CLOSED — this writes to the DB directly; there is no price importer)
python tools/standard_life_pdf_to_qif.py statement.pdf --load-prices ~/path/to/my.mfl
```

The funds have no ticker, so they can never be auto-priced. The tool recovers a
price history by solving each Lifestyle switch as a linear system — a switch
liquidates and rebuys the *whole* plan, so it states the plan's value and the
units on both sides. See ADR-153; the reasoning matters if you touch this.

**It refuses to write unless the replayed closing position matches the
statement's own investment summary, to the unit.** Don't remove that check — the
conversion is inference, and that is what makes it trustworthy.

Two things to know before re-running it:

- **Follow-up statements re-list history you already imported.** These rows carry
  no provider IDs, so the importer cannot dedupe them and re-importing an
  overlapping period **double-counts**. Use `--since <date>` plus `--hold
  FUND=UNITS` for the position you already hold. Omitting `--hold` is a hard
  error, not a warning: without it the tool would happily invent the missing
  opening position as "policy credits" and still reconcile.
- **`--contrib-cash` is the one foot-gun.** It emits a `Contrib` row funding each
  `Buy`, for an *empty* account. If the contributions are already in the account
  as cash rows, it doubles the money — and the reconciliation check will not
  catch it, because it checks units, not cash.

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
