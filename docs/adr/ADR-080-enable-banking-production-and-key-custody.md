# ADR-080 — Enable Banking production activation & application-key custody (planning; custody decision deferred)

**Date:** 2026-06-16
**Status:** Proposed (planning). The **BYO-vs-hosted key-custody** choice (decision F1a of the 1.0 launch plan) is **deferred** pending the workstream-B3 compliance read; 1.0 ships the existing BYO cores unchanged. This ADR frames the decision, fixes the constraints that hold either way, and records what triggers the accept-decision (to be written as an amendment).
**Related:** ADR-077 (bank-feeds framework + the shipped BYO provider cores, incl. Enable Banking), ADR-035 (`setting` table for keys), ADR-079 (pricing — the hosted model could reopen subscription), ADR-078 (distribution — the non-sandboxed channel keeps local custody viable), `docs/RELEASE_1.0_BACKLOG.md` workstreams F + B.

---

## Context

ADR-077 shipped Qt-free provider cores, including **Enable Banking** (the free UK/EU Open Banking feed, BYO-credentials). **Automated feeds are a 1.1 fast-follow, not a 1.0 feature** — so nothing here blocks launch. What *does* need framing is the path to **production** and the open question it forces.

**Enable Banking "Production / full activation" requires** (their Control Panel docs): a verified **contract**, completed **KYC**, a **billing account**, an **application description shown to end users at consent**, a **data-protection email**, a **privacy-policy URL**, and a **terms-of-service URL**. None exist until workstreams **B** (company, legal) and **W** (website) complete — and the privacy/ToS URLs are needed for launch *anyway*, so they double-count.

**The load-bearing open question (F1a): who holds the Enable Banking application registration and its private key (`.pem`)?**

### Options considered
1. **BYO — each user registers their own Enable Banking application and supplies the key** (what the shipped cores assume). Maximum privacy; MFL is a local tool, **not** a data controller or a regulated/agent entity; lightest compliance. Cost: real setup friction (the user navigates the EB Control Panel once).
2. **MFL-hosted shared registration** — MFL ships one registration; the user just clicks "connect." Smooth UX, but MFL becomes a **data-controller / TPP-adjacent** entity with materially heavier **UK/EU GDPR + KYC + PSD2** obligations and needs **server-side custody** of the key — which introduces real infrastructure + cost, and would likely **reopen subscription pricing** (ADR-079's escape hatch).
3. **Hybrid** — BYO by default, an optional hosted "convenience" path later for users who'll pay for less setup.

---

## Decision

**Defer the custody choice to after the B3 compliance read. Ship 1.0 with the BYO cores unchanged** (feeds are 1.1; there is nothing to decide for launch). Record the constraints that hold regardless, and the trigger to make the call.

### Constraints that hold under *any* custody choice
- **The `.pem`/application key must never live in the shipped binary or in the `.mfl` file.** Under BYO it is the *user's* key, stored in the **OS keychain** (the F2 backlog item — credentials move out of `setting`/`.mfl` once non-technical users hold real bank tokens). Under hosted it would require server-side custody — a decision with its own security review.
- **Enable Banking consents expire (~90 days)** → a re-consent / re-auth flow is required either way (workstream F2), without losing the account link.
- **Production needs the website's privacy-policy + ToS URLs + a data-protection email** (workstreams B2/W2) — these are launch deliverables anyway.
- **The non-sandboxed direct-distribution choice (ADR-078)** keeps *local* key custody and the file-based model viable; a hosted model would not depend on it but would add a server surface.

### Activation path (Restricted → Unrestricted)
1. **Restricted mode first** — activate by **whitelisting the owner's own bank accounts** (HSBC UK etc.), so the full consent → fetch → `stage_feed` → review → commit round-trip can be **owner-tested end-to-end before any manual review**.
2. **Then request Unrestricted (full) activation** — triggers Enable Banking's **manual review + KYC + contract + billing-account** association. Gate this on B1 (entity), B2 (legal docs/URLs), and the B3 compliance determination.

### Trigger to make the F1a call (and write the ADR-080 amendment)
The custody decision is made when **B3 (PSD2/AISP role determination) + B1 (entity) + B2 (privacy/ToS live)** are done — i.e. when we actually know whether the hosted model carries regulated-entity obligations MFL is willing to take on. Until then, **do not architect against either option**: keep the provider cores BYO-compatible and credential-source-agnostic.

---

## Consequences

### Positive
- **1.0 is unblocked** — no custody decision needed to launch; the BYO cores already work.
- The **activation path is de-risked** by Restricted mode (own-account testing before full review).
- Keeping the key out of the binary/`.mfl` and consents re-auth-able is **correct under either future**, so the F2 work is not wasted whichever way F1a lands.

### Negative / trade-offs
- **BYO setup friction persists** for 1.1 feeds (the user registers their own EB app) — accepted as the privacy/compliance-simplest default; the hybrid option can soften it later.
- The decision is **genuinely blocked on legal input** (B3), not engineering — so the timeline depends on workstream B.
- A later **hosted** choice would cascade: data-controller status, KYC of *our* company beyond EB's, server-side key custody, and a likely **subscription** to fund it (reopening ADR-079).

### Ongoing responsibilities
- Keep provider cores **BYO-compatible and credential-source-agnostic** so a hosted option remains addable without rework.
- Move feed credentials to the **OS keychain** (F2) before 1.1 real-user feeds.
- Implement the **~90-day re-consent** flow (F2).
- Write the **accept-decision as an ADR-080 amendment** once B3 lands, recording the chosen custody model and its compliance posture.
