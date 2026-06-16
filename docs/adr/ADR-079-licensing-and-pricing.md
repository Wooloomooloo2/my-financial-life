# ADR-079 — Licensing & pricing: one-time perpetual, everything included, offline license key

**Date:** 2026-06-16
**Status:** Accepted (pricing model = decision C1 of the 1.0 launch plan, locked 2026-06-16). The **license-enforcement mechanism** (offline signed key) is the recommended design; its details are settled here but finalised in implementation.
**Related:** ADR-016 (local-first auto-commit file), ADR-050 (local-first, cross-platform), ADR-077 (BYO bank-feed credentials → no central cost), ADR-078 (direct distribution → Merchant-of-Record payments), ADR-080 (Enable Banking custody — the one path that could reopen this), `docs/RELEASE_1.0_BACKLOG.md` workstream C.

---

## Context

1.0 is a paid product (workstream C). Three things need deciding: the **pricing model**, the **license-enforcement mechanism**, and the **payment path** for direct sales.

The decisive fact: **MFL is local-first with BYO feed credentials (ADR-077) — there is no ongoing server or per-user cost to amortise.** The app talks only to third-party APIs the *user* holds keys for (their bank feed provider, openexchangerates, Tiingo). That removes the usual justification for a subscription and points at a one-time purchase.

### Options considered (pricing)
- **A — One-time perpetual, everything included; paid major-version upgrades (CHOSEN).** Simplest to message and license; matches "no recurring cost → no recurring charge."
- **B — Tiered unlock** (base + a paid "Plus" for premium providers like SimpleFIN/Plaid). Rejected — adds licensing complexity and store-IAP entanglement, and with no marginal cost per provider there's nothing to meter; everything-included is cleaner.
- **C — Subscription.** Rejected for the local-first product — churn-prone and hard to justify with no server cost. *Only* becomes defensible if MFL takes on hosted infrastructure (the ADR-080 hosted-Enable-Banking model); kept as an explicit escape hatch, not the plan.

---

## Decision

### Pricing
**A single one-time perpetual purchase (~£25–45 / $30–50), with everything unlocked** — all feed providers (incl. SimpleFIN/Plaid), all reports, investments, multi-currency. **Major versions (2.0) are a new paid upgrade**; 1.x keys keep working on 1.x forever. This supersedes the earlier "willing-to-pay unlocks SimpleFIN/Plaid" framing — all providers are in the box; the user still supplies their own provider key/cost under BYO.

### License-enforcement mechanism — offline signed key
**An offline, locally-verified license key. No activation server.**
- The app ships a **public key**; a purchased license is a small token **signed by our private key** (held by the fulfilment side / Merchant-of-Record), validated entirely on-device. This fits local-first + privacy (no phone-home) and needs **no backend**.
- The key encodes the buyer's name/email + an **edition/version entitlement** (e.g. "1.x"), so a 2.0 upgrade is simply a new key while old keys keep validating against 1.x.
- New **Qt-free `licensing.py`** (verify-only — never holds the private key) + an **"Enter license / Buy"** flow + license state shown in the **About** box.
- **Trial:** a time-limited **full-feature** trial (converts well for utilities), unlocked by entering a purchased key. (Recommended; exact trial length finalised in implementation.)

### Payments (direct sales) — Merchant-of-Record
Use a **Merchant-of-Record** (Paddle / Lemon Squeezy / FastSpring), not bare Stripe. The MoR becomes the **seller of record** and handles **UK/EU VAT + US sales tax + invoicing + license-key delivery** — the single biggest tax-compliance relief for a solo founder. (Cross-ref ADR-078: direct distribution means *we* would otherwise owe cross-border tax; the MoR removes that.)

### Stores (1.1+, deferred)
When the App Store channel lands (ADR-078), store policy may **mandate the store's IAP** for digital unlocks (15–30% cut, no out-linking to a cheaper checkout). The license *model* is unchanged — a store purchase grants the **same entitlement** as a direct key; we accept the store cut on that channel. Reconciled when K3 starts.

---

## Consequences

### Positive
- **Simplest possible message and license** ("buy once, everything included").
- **No backend** — offline keys keep the app local-first and privacy-clean, with nothing to run or breach.
- **MoR offloads tax/VAT** entirely; **upgrade revenue** preserved via paid 2.0.
- Reinforces the product story: a private, you-own-it tool, not a data-harvesting subscription.

### Negative / trade-offs
- **Offline keys are crackable** — accepted: the target audience is honest non-technical buyers; the goal is gentle friction, not DRM fortress. (A future online activation can be added if piracy ever bites.)
- **No recurring revenue** — fine, because there's no recurring cost; growth comes from new sales + paid upgrades.
- **Version-entitlement bookkeeping** — we must track which key edition unlocks which major version.
- **MoR takes ~5%+** of revenue — the price of outsourced tax compliance.

### Ongoing responsibilities
- **Protect the license-signing private key** (held by fulfilment/MoR, never in the app or repo).
- Set the **2.0 upgrade price/policy** when 2.0 is real (out of scope now).
- **Store-IAP reconciliation** is deferred to the K3 round; keep the entitlement check store-agnostic so a store receipt can grant the same unlock.
- Keep the escape hatch in mind: if ADR-080 ever chooses a **hosted** Enable-Banking model (real server cost), revisit subscription for that capability.
