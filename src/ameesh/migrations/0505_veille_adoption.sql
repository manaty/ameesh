-- SPDX-License-Identifier: AGPL-3.0-only
-- #4293 (#3264 C) : adoption d'une version de harnais ou de modèle.
--
-- `agent_registry.harness_bin` : le binaire d'un harnais pour UN agent, à la
-- place de `harness_bins` (configuration de l'hôte) et du PATH. NULL = aucun
-- remplacement, comportement inchangé. C'est le geste du canari et de la
-- bascule hôte par hôte ; le tour SUIVANT le lit, un tour en cours ne change
-- jamais de binaire sous lui (l'exécuteur relit la ligne de l'agent au début
-- de chaque tour). La valeur est validée par `ameesh set harness_bin=` : un
-- chemin absolu ; l'exécuteur refuse un binaire absent (hôte non prêt).
--
-- `harness_adoptions` : l'état mécanique d'une adoption, par lot d'évaluation.
-- Le rapport de rejeu (lot B) y est figé avec son empreinte ; `state` porte la
-- séquence (canari, hôte suivant, valeurs inverses). Les transitions sont
-- aussi notées au journal du lot (`harness-adoption:`) pour la relecture. La
-- table n'a pas de clé étrangère : un lot fermé garde la trace de son
-- adoption, et une adoption ne s'écrit que sur un lot existant (vérifié avant).
CREATE TABLE IF NOT EXISTS harness_adoptions (
    lot_id        bigint PRIMARY KEY,
    project       text NOT NULL DEFAULT '',
    harness       text NOT NULL,
    object        text NOT NULL DEFAULT 'harness'
                  CHECK (object IN ('harness', 'model')),
    from_version  text NOT NULL,
    to_version    text NOT NULL,
    stage         text NOT NULL,
    report        jsonb NOT NULL,
    report_digest text NOT NULL,
    state         jsonb NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at    timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS harness_adoptions_stage_idx
    ON harness_adoptions (stage);

ALTER TABLE agent_registry ADD COLUMN IF NOT EXISTS harness_bin text;
