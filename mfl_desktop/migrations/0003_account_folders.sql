-- 0003_account_folders.sql — adds account folders for sidebar organisation.
-- See ADR-015. Folders are flat (no nesting). An account either belongs to a
-- folder (folder_id NOT NULL) or sits at the sidebar root (folder_id IS NULL).
-- Deleting a folder lets its accounts fall to root via ON DELETE SET NULL.

PRAGMA foreign_keys = ON;

CREATE TABLE account_folder (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    archived_at TEXT
);

CREATE INDEX idx_account_folder_sort ON account_folder(sort_order);

ALTER TABLE account ADD COLUMN folder_id INTEGER
    REFERENCES account_folder(id) ON DELETE SET NULL;

CREATE INDEX idx_account_folder ON account(folder_id);
