# ADR-004 — Cross-platform portability approach

**Date:** 2026-05-18
**Status:** Accepted

---

## Context

My Financial Life must run correctly on Windows, macOS, and Linux from a single codebase. Platform differences — file path separators, data directory conventions, line endings — must be handled systematically rather than case-by-case.

---

## Options considered

### Option 1 — Enforced portability conventions (chosen)
Define a set of explicit conventions that all contributors must follow, making platform-specific code a detectable violation rather than an accidental omission. Consistent with My Retirement Life ADR-004.

### Option 2 — Windows-first, port later
Rejected. Retrofitting portability after the fact is expensive and error-prone. The target demographic includes macOS users disproportionately (consistent with MRL's observation) so macOS support is a day-one requirement.

---

## Decision

**Enforce the following portability conventions throughout the codebase, consistent with My Retirement Life ADR-004:**

- **`pathlib.Path`** for all file path construction — never string concatenation with `/` or `\\`
- **`platformdirs`** for OS-appropriate data directory resolution (user data, config, cache)
- **`.gitattributes`** enforcing LF line endings for all text files
- **`python-dotenv`** for environment-based configuration — no hardcoded paths or environment assumptions
- **No platform-specific runtime dependencies** — any library that requires OS-specific binaries must have a cross-platform alternative

---

## Consequences

- The Oxigraph store is created in the correct OS data directory automatically via `platformdirs.user_data_dir()`.
- File paths in templates and API responses always use forward slashes (handled by `pathlib`).
- Development on Windows produces commits that are safe to check out on macOS and Linux.
- A `devcontainer.json` may be added in future for optional Linux development on Windows via WSL.
