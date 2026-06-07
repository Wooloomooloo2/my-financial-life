-- 0010_reports.sql — saved reports + per-section folder table (ADR-039).
--
-- Two new tables, no changes to existing schema:
--
--   report_folder — sidebar folder for the new Reports section. Mirrors
--                   account_folder's shape (flat, no nesting, sort_order
--                   for explicit ordering). Lives in its own table per
--                   ADR-039 §folder-model — folders are namespaced by the
--                   sidebar section they belong to.
--   report        — one row per saved report instance. `type` discriminates
--                   the per-type filter schema; `filters_json` carries the
--                   per-type filter blob (parsed via mfl_desktop/reports/
--                   filters.py at read time). Adding a new report type is
--                   one new enum value + one new filter dataclass, no
--                   migration.
--
-- IRIs follow the ADR-006 convention (mfl:Report_<uuid8>,
-- mfl:ReportFolder_<uuid8>). Name uniqueness is per-folder, matching the
-- account / account-folder pattern.

PRAGMA foreign_keys = ON;

-- ─────────────────────────────────────────────────────────────────────────────
-- report_folder — sidebar folder for the Reports section.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE report_folder (
    id          INTEGER PRIMARY KEY,
    iri         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    archived_at TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_report_folder_sort ON report_folder(sort_order);

-- ─────────────────────────────────────────────────────────────────────────────
-- report — saved report instance. filters_json is opaque to SQL; parsed
-- per type by mfl_desktop/reports/filters.py. The type enum is the only
-- thing here that knows what's inside filters_json.
--
-- folder_id NULL means the report sits at the Reports-section root (same
-- semantics as account.folder_id NULL → sidebar root).
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE report (
    id           INTEGER PRIMARY KEY,
    iri          TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    type         TEXT NOT NULL
                 CHECK (type IN (
                     'spending_over_time',
                     'net_worth',
                     'income_expense',
                     'sankey'
                 )),
    folder_id    INTEGER REFERENCES report_folder(id) ON DELETE SET NULL,
    filters_json TEXT NOT NULL DEFAULT '{}',
    archived_at  TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(name, folder_id)
);

CREATE INDEX idx_report_folder ON report(folder_id);
CREATE INDEX idx_report_type   ON report(type);
