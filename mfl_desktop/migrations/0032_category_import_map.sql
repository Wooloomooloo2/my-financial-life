-- 0032_category_import_map.sql — stop imports from recreating cleaned-up
-- categories (ADR-112).
--
-- Importers resolve each transaction's source category path (e.g.
-- "Personal:Clothing") through find_or_create_category_path, which *creates*
-- any path it doesn't find. A user who curates their tree (merging, reparenting,
-- deleting categories) has those decisions silently undone on the next import —
-- the source path no longer matches, so a duplicate is recreated.
--
-- Two pieces, mirroring the payee-alias model (ADR-028):
--   1. `category_import_map` — a persistent "source path -> my category" map,
--      auto-recorded when the user merges/deletes/reparents a category and
--      consulted at import before any create. Keyed on the normalised source
--      path string, so it sidesteps the category hierarchy entirely.
--   2. A "Needs Review" holding category — where unmatched import categories
--      land when match-only mode (setting `import_match_only_categories`) is on,
--      so genuinely-new imports don't hide among real Uncategorised items.

CREATE TABLE category_import_map (
    -- Normalised source path: lower-cased, ':'-joined, segments trimmed.
    source_path TEXT PRIMARY KEY,
    -- Where transactions carrying this source path should land. If the target
    -- category is later deleted the mapping is meaningless, so it cascades away.
    target_category_id INTEGER NOT NULL
        REFERENCES category(id) ON DELETE CASCADE
);

CREATE INDEX idx_category_import_map_target
    ON category_import_map(target_category_id);

-- The "Needs Review" holding category (kind 'expense', like Uncategorised). Its
-- id is recorded in `setting` so a later rename can't orphan the lookup.
INSERT INTO category (parent_id, name, source, kind)
VALUES (NULL, 'Needs Review', 'system', 'expense');

INSERT OR REPLACE INTO setting (key, value)
VALUES (
    'needs_review_category_id',
    (SELECT id FROM category
      WHERE name = 'Needs Review' AND source = 'system' AND parent_id IS NULL)
);
