-- SPDX-License-Identifier: AGPL-3.0-only
-- #4167 (manaty/ameesh#3185, volet 1) : liens typés entre lots, en plus des
-- prérequis `work_item_dependencies` (L385). Même journal (work_item_events),
-- même verrou de graphe ; aucun lien n'est déduit d'une note ou d'un courrier.
--   duplicates     : work_item_id est un doublon de other_id (orienté)
--   supersedes     : work_item_id remplace other_id (orienté)
--   part_of        : work_item_id est une part de other_id (orienté)
--   conflicts_with : symétrique, rangé work_item_id < other_id ; tant que les
--                    deux lots sont ouverts et qu'aucune décision n'est
--                    consignée sur le lien, une alerte `lot_conflict_undecided`.
-- Un cycle est refusé dans chaque graphe orienté (par type).
CREATE TABLE IF NOT EXISTS work_item_links (
    work_item_id bigint NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
    other_id     bigint NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
    kind         text NOT NULL
                 CHECK (kind IN ('duplicates', 'supersedes', 'part_of', 'conflicts_with')),
    created_by   text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- décision consignée sur un conflit (texte, auteur, instant) ; NULL sinon
    decision     text CHECK (decision IS NULL OR btrim(decision) <> ''),
    decided_by   text,
    decided_at   timestamptz,
    PRIMARY KEY (work_item_id, other_id, kind),
    CHECK (work_item_id <> other_id),
    CHECK (kind <> 'conflicts_with' OR work_item_id < other_id),
    CHECK (kind = 'conflicts_with' OR decision IS NULL)
);
CREATE INDEX IF NOT EXISTS work_item_links_other ON work_item_links(other_id);
