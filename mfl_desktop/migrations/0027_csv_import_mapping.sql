-- ADR-021 follow-up / ADR-077 Track 1: saved CSV column-mapping profiles.
--
-- When a generic CSV is imported, the user maps its columns once. The mapping
-- is keyed by a "header signature" (the normalised header row), so a later
-- import of the same export format auto-applies it and skips the wizard.
-- Account-agnostic: a bank's CSV layout is the same whichever account it
-- feeds, so one profile serves all of them.

CREATE TABLE csv_import_mapping (
    id           INTEGER PRIMARY KEY,
    signature    TEXT NOT NULL UNIQUE,   -- normalised header row
    mapping_json TEXT NOT NULL,          -- serialised CsvColumnMapping
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at TEXT NOT NULL DEFAULT (datetime('now'))
);
