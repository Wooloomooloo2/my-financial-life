# ADR-164 — A report leads with what it is, not with "Untitled"

**Date:** 2026-07-13
**Status:** Implemented
**Related:** ADR-161/162/163 (the same design review). ADR-119 (`PageHeader`). ADR-039 (the saved-report framework). ADR-084 (consolidate divergent duplicates of the same thing — the rule this follows).

## Context

From the design review. Open any report from the Reports menu and the biggest text on the screen is:

> **Untitled**
> *Spending Over Time*

The word "Untitled" is a statement about the **file** — it means "you have not saved this yet". It is not what you are *looking at*. Meanwhile the thing you *are* looking at, and the only thing that distinguishes this window from four other report windows, is relegated to a small grey subtitle.

Every report opened from the menu is unsaved, so this is the state most reports are in most of the time: the app's most prominent word, on a screen the user opened deliberately, is a word about nothing.

Five report windows (Spending/Income Over Time, Cash Flow, Income & Expense, Category & Payee, Investment Returns) had each grown their own copy of the same title-building logic — the folder prefix, the dirty asterisk, the "Untitled" fallback, and the window title. Five copies of one idea.

## Decision

**Invert the heading, and give it one definition.**

- **Unsaved:** title = the report's *type* ("Spending Over Time"), subtitle = "Unsaved report".
- **Saved:** title = the *name the user gave it*, subtitle = the type. (Unchanged — a name the user chose genuinely does outrank the type.)

The report's type is its identity until it has a name. Unsaved-ness is a real state and still worth showing, but it is a footnote, not a headline.

New `page_header.report_heading(type_label, loaded_name, folder_name=, dirty=)` returns `(title, subtitle, window_title)` — the single definition, per the ADR-084 rule. `page_header.report_folder_name(repo, folder_id)` collapses the same four-line scan over `list_report_folders()` that all five windows had inlined.

## Rejected

- **Keeping "Untitled" but shrinking it to the subtitle.** Better, but it still spends the subtitle line on a non-fact. "Unsaved report" says the same thing and is a statement about the report rather than about the absence of a filename.
- **A dirty-style asterisk or a badge widget on the type.** The unsaved state has no *edit* to mark — a bare report isn't dirty, it simply doesn't exist on disk yet. A word is clearer than a glyph here, and the asterisk already means something else (a saved report with unsaved changes).
- **Leaving the five copies alone and just changing the string in each.** Five places to get right, and the next report window would copy whichever one it found first.

## Consequences

- Five report windows now share one heading definition; four of them lost their inlined folder-name scan too.
- Window titles (taskbar / alt-tab) change for unsaved reports: "Spending Over Time — Untitled" → "Spending Over Time — Unsaved".
- A saved report is entirely unaffected — same title, same subtitle, same asterisk, same folder prefix.

`tests/test_report_heading.py` 7/7 (the unsaved report leads with its type; the word "Untitled" appears nowhere; a saved report leads with its name; the dirty mark; the folder prefix; folder + dirty composing; and a stray folder name on an *unsaved* report not leaking into the title, since an unsaved report is in no folder). Full suite 331/331. No schema change.
