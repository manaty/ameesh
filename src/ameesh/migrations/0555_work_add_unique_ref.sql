-- SPDX-License-Identifier: AGPL-3.0-only
-- 0555_work_add_unique_ref — #6090 (manaty/ameesh#4642, écart 2) : la clé
-- d'idempotence durable de `ameesh work add --unique-ref`.
--
-- Un exécutant externe (adaptateur IMA) rejoue une création après une réponse
-- perdue ou un redémarrage ; la clé qu'il fournit est écrite sur le lot, et
-- un second `work add` avec la même clé rend ce lot au lieu d'en créer un
-- autre. L'unicité vaut pour tous les états : une clé consommée par un lot
-- fermé rend ce lot, jamais un second.
--
-- La clé est opaque et globale au mesh : c'est à l'appelant de l'espacer par
-- source (`ima:<ticket>`). Plusieurs lots peuvent légitimement partager une
-- `issue_ref` (les sous-lots d'un même ticket) ; ils ne peuvent pas partager
-- une `unique_ref`.
--
-- Additif seulement : colonne nullable (NULL = création ordinaire, aucune
-- ligne existante ne change de sens), index unique partiel sur les seules
-- lignes qui portent une clé. Construction simple, pas `CONCURRENTLY`
-- (`migrate()` tourne dans une transaction, comme 0497) ; attente de verrou
-- bornée, la table des lots est très écrite.
SET LOCAL lock_timeout = '5s';

ALTER TABLE work_items ADD COLUMN IF NOT EXISTS unique_ref text;

CREATE UNIQUE INDEX IF NOT EXISTS work_items_unique_ref_idx
    ON work_items (unique_ref)
    WHERE unique_ref IS NOT NULL;

COMMENT ON COLUMN work_items.unique_ref IS
    '#6090 durable caller-supplied idempotency key carried by a lot (NULL means an ordinary lot). Exact match, any state, global to the mesh.';
COMMENT ON INDEX work_items_unique_ref_idx IS
    '#6090 at most one lot per key. Partial so ordinary lots (NULL) are unaffected.';
