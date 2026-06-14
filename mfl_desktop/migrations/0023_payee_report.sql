-- ADR-066: add the 'payee' report type (Arc E, E2 — Payee report).
--
-- The report.type CHECK constraint (0010_reports.sql, last widened in
-- 0014_investment_returns_report.sql) hard-lists the allowed type strings,
-- and SQLite can't ALTER a CHECK in place, so we recreate the table with
-- the widened list — the same approach 0014 used. report.folder_id
-- references report_folder; foreign_keys=OFF prevents intermediate checks
-- from firing during the swap, and the FK resolves to the recreated table
-- once the rename completes. Nothing references report by FK.

PRAGMA foreign_keys = OFF;

CREATE TABLE report_new (
    id           INTEGER PRIMARY KEY,
    iri          TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    type         TEXT NOT NULL
                 CHECK (type IN (
                     'spending_over_time',
                     'net_worth',
                     'income_expense',
                     'sankey',
                     'investment_returns',
                     'payee'
                 )),
    folder_id    INTEGER REFERENCES report_folder(id) ON DELETE SET NULL,
    filters_json TEXT NOT NULL DEFAULT '{}',
    archived_at  TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(name, folder_id)
);

INSERT INTO report_new (
    id, iri, name, type, folder_id, filters_json, archived_at, created_at
)
SELECT
    id, iri, name, type, folder_id, filters_json, archived_at, created_at
FROM report;

DROP TABLE report;
ALTER TABLE report_new RENAME TO report;

-- Recreate the indexes lost with the original table.
CREATE INDEX idx_report_folder ON report(folder_id);
CREATE INDEX idx_report_type   ON report(type);

PRAGMA foreign_keys = ON;
