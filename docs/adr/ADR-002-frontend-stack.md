# ADR-002 — Frontend stack

**Date:** 2026-05-18
**Status:** Superseded by [ADR-008](ADR-008-desktop-ui-framework.md) (2026-06-05)

---

## Context

My Financial Life needs a frontend stack for a data-dense application: account registers, transaction lists, dashboard charts, import workflows, and category management. The stack must work without a separate build pipeline and must be maintainable by a small team.

---

## Options considered

### Option 1 — HTMX + Tailwind CSS + DaisyUI + Chart.js (chosen)
Consistent with My Retirement Life. HTMX enables server-driven partial page updates from FastAPI without a JavaScript framework or build step. Tailwind provides utility-first styling. DaisyUI adds a complete component library with built-in light/dark theming on top of Tailwind. Chart.js handles data visualisation. All frontend assets are served from CDN — no npm, no webpack, no node_modules.

### Option 2 — React or Vue
Rejected. Requires a separate JavaScript build pipeline, a separate development server, and a JavaScript codebase alongside the Python codebase. Inconsistent with MRL and significantly increases project complexity for no benefit at this scale.

### Option 3 — Plain HTML + vanilla JavaScript
Rejected. Achievable but results in verbose, hard-to-maintain code for the interactive patterns required (inline transaction editing, live search, HTMX-style partial updates). DaisyUI eliminates the need to hand-craft component styling.

---

## Decision

**HTMX + Tailwind CSS + DaisyUI + Chart.js, consistent with My Retirement Life.**

---

## Consequences

- No JavaScript build toolchain — frontend assets loaded from CDN in development and bundled with PyInstaller for distribution.
- Server renders all HTML via Jinja2 templates; HTMX handles partial updates without full page reloads.
- DaisyUI provides light/dark theme toggle out of the box.
- Chart.js provides the dashboard spending trend, category breakdown, and net worth charts.
- Stack consistency with MRL means templates and frontend patterns transfer directly between projects.
