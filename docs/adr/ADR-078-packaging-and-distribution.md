# ADR-078 — Packaging & distribution: direct signed + notarised downloads first; App Stores deferred

**Date:** 2026-06-16
**Status:** Accepted (decision K0 of the 1.0 launch plan, locked 2026-06-16).
**Amends:** ADR-003 (packaging strategy — settles the deferred PyInstaller-vs-Briefcase tool choice and the "signing TBD"), ADR-008 ("Windows-first" already retired by ADR-050).
**Closes:** ADR-050 **Tier-3** ("packaging — its own round; own ADR when started").
**Related:** ADR-050 (cross-platform first-class, Tier-1/2 done), ADR-016 (auto-commit `.mfl` file model), ADR-057 (`Snapshots/` beside the live file), ADR-079 (pricing/payments — Merchant-of-Record), `docs/RELEASE_1.0_BACKLOG.md` workstream K.

---

## Context

1.0 must be a **downloadable, trustworthy, paid** app on macOS + Windows (decision K0 of the launch plan). The owner's phrasing was "notarised … so it's seen as legit and safe," which conflates two separable things:

- **Trust** — on macOS a **Developer ID-signed + notarised** `.app`/DMG passes Gatekeeper with no "unidentified developer" wall (the exact bar ADR-050 set); on Windows a **code-signed** installer seasons SmartScreen. **Neither requires an App Store.**
- **Store presence** — the Mac App Store / Microsoft Store add a store badge, auto-update, and store-handled payment/tax, at the cost of review, store cut (15–30%), and — critically on the **Mac App Store — sandboxing**.

**The sandbox is fundamentally at odds with MFL's file model.** MFL opens *arbitrary* `.mfl` files anywhere (ADR-016), writes `Snapshots/` (ADR-057) and `Library/` folders *beside* the live file, defaults to an AppData path with a cwd bridge (ADR-050 Tier-2). The Mac App Store sandbox forbids all of that without **security-scoped bookmarks** for every user-chosen file and relocating the sidecar folders into the sandbox container — a substantial rework. Direct distribution has none of these constraints.

### Options considered
1. **Direct signed + notarised downloads for 1.0; App Stores as a 1.1/1.2 channel (CHOSEN).** Fastest path to full trust, zero sandbox rework, no store cut, keeps the file model intact.
2. **App Stores from day one.** Rejected for 1.0 — puts the MAS sandbox rework on the launch critical path and concedes 15–30% before the product has proven it sells.
3. **Both in parallel.** Rejected — pays the direct-signing cost *and* the sandbox cost simultaneously for no launch benefit.

---

## Decision

**Ship 1.0 as direct, signed, notarised downloads from our own website. Defer both App Stores to 1.1/1.2 as a separate channel.** This removes the sandbox work from the 1.0 critical path entirely (the single biggest scope lever in the launch plan).

### Build toolchain (settles ADR-003's deferral)
- **PyInstaller, per-OS**, consistent with ADR-003's chosen approach and with MRL. macOS: one-dir `.app` → DMG. Windows: one-dir → installer. (Briefcase/BeeWare was considered for its built-in dmg+sign+notarise automation; rejected to keep one toolchain we already understand and to avoid re-tooling the build — we wire signing/notarisation into the pipeline ourselves.)
- **Architectures:** macOS **universal2** (Apple Silicon + Intel); Windows **x64**. (Linux/AppImage from ADR-003 stays possible but is **out of 1.0 scope** — no owner demand.)
- App identity is already set (ADR-050 Tier-2: `setApplicationName("MFL")` → AppData path); add a reverse-DNS bundle identifier (e.g. `com.<org>.myfinanciallife`) for signing.

### macOS — Developer ID + notarisation
- Apple Developer Program enrolment under the company (ADR-079 / workstream B1) — **$99/yr**.
- **Developer ID Application** certificate; **hardened runtime** with the minimum entitlements MFL needs: outbound network (FX/openexchangerates, Tiingo prices, bank feeds), user-selected file read/write — **no `app-sandbox` entitlement** (direct distribution).
- `codesign` → `notarytool` submit → **staple** → DMG (also signed/notarised). Verify a clean Gatekeeper pass on a machine that never saw the build.
- **Auto-update:** Sparkle, with an appcast feed hosted on the website (workstream W).

### Windows — code signing
- **Code-signing certificate** — OV now effectively needs a hardware token / cloud HSM; **EV** smooths SmartScreen reputation. Budget ~$200–400/yr (or **Azure Trusted Signing** if eligible).
- Signed installer (Inno Setup) or signed **MSIX**; allow SmartScreen reputation to season post-launch.
- **Auto-update:** WinSparkle (Inno) or the MSIX App Installer update feed.

### Stores — deferred, documented (1.1/1.2)
When revenue justifies it, a separate round adds: **Mac App Store** (sandbox entitlements, security-scoped bookmarks for user `.mfl` files, relocate `Snapshots/`/`Library/` into the container, App Store Connect listing, review) and **Microsoft Store** (MSIX, Partner Center, certification). Store purchases must grant the **same license entitlement** as direct sales (ADR-079), and any store-mandated IAP cut is accepted there.

---

## Consequences

### Positive
- **Full "legit & safe" trust without store gatekeeping** — Gatekeeper/SmartScreen pass on a double-click.
- **Zero sandbox rework in 1.0**; the ADR-016/057 file model (arbitrary `.mfl`, sidecar folders, cwd bridge) is retained untouched.
- **No store cut** on direct sales; **closes ADR-050 Tier-3** with one build toolchain across both OSes.

### Negative / trade-offs
- **We run our own signing, notarisation, and update infrastructure** (certs, notarytool, appcast/WinSparkle feeds).
- **Windows cert cost + SmartScreen seasoning lag** for a brand-new publisher (early users may see a SmartScreen prompt until reputation builds).
- **No store auto-discovery** — distribution leans entirely on the website (workstream W).
- **We are responsible for payment + tax** on direct sales → handled by a Merchant-of-Record (ADR-079).

### Ongoing responsibilities
- Renew Apple ($99/yr) + Windows signing certs; **notarise every macOS release**; keep the update feeds current with each release.
- Hold the line on the **portability rule set** (ADR-050 pitfall #8) so the per-OS builds stay byte-clean.
- Revisit the App Store channel (and its sandbox rework) when revenue justifies it; until then keep new file-access code sandbox-*aware* (no gratuitous absolute paths) so the future port is not made harder.
