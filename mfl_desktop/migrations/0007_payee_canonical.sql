-- 0007_payee_canonical.sql — payee aliasing model (ADR-028 / ADR-029 round 1).
-- A payee row is now either:
--   * canonical (canonical_id IS NULL) — the user's preferred label, with
--     metadata (default_category_id) and visible in the typeahead.
--   * an alias (canonical_id IS NOT NULL) — a stored historical label that
--     routes to a canonical for display + reporting purposes. Hidden from
--     the typeahead. Aliases of aliases are not allowed; enforced at the
--     Repository layer (SQLite CHECK isn't expressive enough).
--
-- Existing rows are all canonical by default (NULL). No backfill needed.
-- ON DELETE SET NULL: if a canonical is hard-deleted, its aliases auto-
-- promote to canonical rather than being orphaned with a dangling FK.

PRAGMA foreign_keys = ON;

ALTER TABLE payee ADD COLUMN canonical_id INTEGER
    REFERENCES payee(id) ON DELETE SET NULL;

CREATE INDEX idx_payee_canonical ON payee(canonical_id)
    WHERE canonical_id IS NOT NULL;
