-- ADR-073: swap the rule.pattern_kind vocabulary to the owner's matcher set.
--
-- The `rule` table (0001) reserved pattern_kind CHECK ('substring','regex'),
-- but the auto-categorisation arc (round 2 / Arc G) wants the friendly set
-- 'contains' / 'starts_with' / 'ends_with' / 'is_exactly'. SQLite can't ALTER
-- a CHECK in place, so we recreate the table — the same recipe the report-type
-- CHECK widenings used (0014 / 0023 / 0024). The table has been unused since
-- 0001, so there is nothing real to copy; the CASE maps any legacy rows
-- defensively. foreign_keys=OFF stops intermediate FK checks during the swap;
-- set_category_id / set_payee_id resolve to the recreated table after rename.

PRAGMA foreign_keys = OFF;

CREATE TABLE rule_new (
    id              INTEGER PRIMARY KEY,
    iri             TEXT UNIQUE NOT NULL,
    pattern         TEXT NOT NULL,
    pattern_kind    TEXT NOT NULL
                    CHECK(pattern_kind IN (
                        'contains', 'starts_with', 'ends_with', 'is_exactly'
                    )),
    match_field     TEXT NOT NULL CHECK(match_field IN ('payee_raw', 'memo')),
    set_category_id INTEGER REFERENCES category(id) ON DELETE CASCADE,
    set_payee_id    INTEGER REFERENCES payee(id) ON DELETE CASCADE,
    priority        INTEGER NOT NULL DEFAULT 100,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (set_category_id IS NOT NULL OR set_payee_id IS NOT NULL)
);

INSERT INTO rule_new (
    id, iri, pattern, pattern_kind, match_field,
    set_category_id, set_payee_id, priority, created_at
)
SELECT
    id, iri, pattern,
    CASE pattern_kind
        WHEN 'substring' THEN 'contains'
        WHEN 'regex'     THEN 'contains'
        ELSE pattern_kind
    END,
    match_field, set_category_id, set_payee_id, priority, created_at
FROM rule;

DROP TABLE rule;
ALTER TABLE rule_new RENAME TO rule;

CREATE INDEX idx_rule_priority ON rule(priority);

PRAGMA foreign_keys = ON;
