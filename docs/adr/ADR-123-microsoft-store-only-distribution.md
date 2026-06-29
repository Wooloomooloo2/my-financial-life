# ADR-123 — Distribution reversal: Microsoft Store-only for 1.0.1

**Date:** 2026-06-29
**Status:** Accepted
**Supersedes:** ADR-078 K0 (the "direct signed/notarised downloads for 1.0; App Stores deferred to 1.1+" decision).
**Touches:** ADR-079 (licensing — largely shelved for the Store channel; the Store owns purchase + entitlement). ADR-122 (the Windows Inno-Setup installer — repurposed as the early-access/sideload channel, not discarded). ADR-050 (cross-platform file rules — the `%APPDATA%` data location matters for MSIX).

## Context

ADR-078 chose **direct signed/notarised downloads** for 1.0 and deferred app stores. After actually pricing the direct path, the owner reweighed it: for a **small app with uncertain, possibly-low revenue**, direct distribution is a stack of **fixed costs paid regardless of sales** —

- an annual Windows code-signing certificate (the CA fee — ~$120–400/yr; hardware-token or managed-service friction since the 2023 hardware-key requirement),
- self-managed **sales-tax** collection/remittance (i.e. an accountant), and
- a registered **legal entity** with published merchant/refund **policies**.

The Microsoft Store converts all of that into a **single variable cut** and acts as Merchant-of-Record: it signs the app, collects and remits tax, processes payments and refunds, and carries the consumer-policy surface. That is the right cost *shape* for an unproven product — you pay only when you earn.

### Verified (web, June 2026)

- **Commission:** **15% for apps** using Microsoft's commerce (12% for games); or use your own commerce and keep ~100% for non-game apps. We use Microsoft commerce → **15%**.
- **Registration:** **free for individual developers** (since June 2025) — no prior $19/$99 fee.
- **Signing:** Store packages are signed by Microsoft — **no third-party code-signing certificate required.**
- **Free keys for testers:** Partner Center can **generate promotional codes** (single-use per tester, or one multi-use code) to give free access to a paid app — explicitly supported for **beta testing**. So early-access volunteers can get the Store build free once it's listed.
- **File access:** `broadFileSystemAccess` is a **restricted capability** requiring extra Store onboarding review + written justification. Likely **avoidable**: file pickers grant brokered access to user-chosen files without it, and the live `.mfl` lives in `%APPDATA%` (allowed). The one behaviour to validate is the auto-write of `Snapshots/`/`Library/` *beside* the live file — under MSIX those auxiliary folders should stay in `%APPDATA%` to avoid needing the restricted capability.

## Decision

**1.0.1 ships Store-only** as the paid, generally-available channel: packaged as **MSIX**, signed by Microsoft, Microsoft as Merchant-of-Record (15% on sales).

- **1.0 (the local Inno-Setup `.exe` from ADR-122) is retained as the early-access / sideload channel** for volunteers *before* the Store listing is live. For a small trusted group, **unsigned is acceptable** (they accept the one-time SmartScreen "Run anyway") — so **no code-signing cert is purchased at all**. ADR-122's installer is repurposed, not wasted.
- **Early-access on the Store after GA:** hand volunteers **promotional codes** for free, auto-updating Store installs.
- **ADR-078 K0 is superseded** (no direct paid channel for 1.0). **ADR-079 is largely shelved** for the Store channel — the Store owns purchase + entitlement, so the own-built Ed25519 license key + third-party Merchant-of-Record aren't needed; the licensing code stays **dormant** (available if a non-Store channel ever returns).
- **Versioning:** **one codebase**, the channel differs only by **version stamp + packaging** — Inno `.exe` at **1.0** (early access) and MSIX at **1.0.1** (Store GA). Do **not** fork the code; bump `version.py` per release.

## Consequences

- Removes the cert cost, the sales-tax/accountant burden, and the legal-entity/merchant-policy surface for 1.0.1 — the operational overhead that didn't fit a small app.
- For non-technical users the Store is *better* than a handed-over installer: no SmartScreen prompt, one-click install, and **automatic updates** (which also closes ADR-078's deferred WinSparkle/auto-update gap for the Store channel).
- **Adds, as the next round's work:** MSIX packaging of the PyInstaller output (parallel to the existing Inno installer — both build from one codebase); Store **certification + policy** compliance; the **15%** cut; and validating the file-access model (prefer pickers + `%APPDATA%`, keep `Snapshots/`/`Library/` in `%APPDATA%` under MSIX to avoid `broadFileSystemAccess`).
- macOS is unaffected and remains out of immediate scope (Windows-first owner).

### Open work for the Store round (a later ADR)
- MSIX packaging recipe for the frozen app (`PyInstaller` output → MSIX), wired alongside `build_windows.ps1`.
- File-access validation under MSIX; relocate auxiliary folders into `%APPDATA%` if needed.
- Partner Center listing (free reserved name, app metadata, age rating, privacy policy URL — `garelochsoft.com`).
- The promo-code flow for early-access volunteers.
