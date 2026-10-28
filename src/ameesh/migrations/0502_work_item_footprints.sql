-- SPDX-License-Identifier: AGPL-3.0-only
-- 0502_work_item_footprints — #4173 (manaty/ameesh#3185, volet 3) :
-- l'empreinte d'un lot, les fichiers ou modules qu'il touche.
--
-- Une ligne par chemin et par source : `intake` (estimée à la création depuis
-- les chemins cités), `manual` (posée par un geste, remplace l'estimation),
-- `diff` (le diff réel de la branche, rafraîchi au gel ou à la demande).
-- L'empreinte d'un lot est l'union de ses lignes. Un chemin désigne un fichier
-- ou un dossier (module) relatif à la racine du dépôt, sans `/` en tête ni en
-- queue : deux chemins se chevauchent s'ils sont égaux ou si l'un est un
-- dossier parent de l'autre (`work_footprints.overlaps`).
--
-- Additif : une table neuve, aucune ligne existante modifiée. Sans ligne, un
-- lot n'a pas d'empreinte connue et l'ordonnancement ne change pas.
CREATE TABLE IF NOT EXISTS work_item_footprints (
    work_item_id bigint NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
    source text NOT NULL CHECK (source IN ('intake', 'manual', 'diff')),
    path text NOT NULL CHECK (path <> '' AND left(path, 1) <> '/'
                              AND right(path, 1) <> '/'),
    head text,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (work_item_id, source, path)
);
