-- SPDX-License-Identifier: AGPL-3.0-only
-- 0560_repair_observation_scopes — #6477 (manaty/ameesh#4981) : les alertes
-- `alert.raised` sans projet sont les constats du mesh lui-même (hôtes,
-- comptes, capacité) ; l'observation U6 les lisait par projet et ne les
-- voyait donc jamais.
--
-- Elles sont désormais reçues sous la portée déclarée par l'appelant (le
-- projet du mesh lu dans la configuration, jamais un nom en dur), mais leur
-- position de balayage ne peut pas être celle du flux
-- `(projet, environnement, 'alerts')` : les alertes qui portent ce projet et
-- celles qui n'en portent aucun sont deux pages distinctes de la même source ;
-- un curseur commun ferait sauter l'une par l'autre.
--
-- Additif seulement : nouvelle table, aucune ligne de `repair_signal_streams`,
-- `repair_signals` ou `repair_signal_refusals` ne change. Un écrivain N-1
-- garde son curseur et ignore cette table ; les reçus restent dans le flux
-- existant, dont le curseur n'avance pas pour cette page.
--
-- Aucun index sur `ameesh_events` : l'index partiel 0557
-- `(project, id) WHERE type='alert.raised'` couvre déjà `project IS NULL`,
-- tant la page ordonnée que le comptage des levées (EXPLAIN vérifié sur la
-- base centrale : Index Scan, 249 lignes, < 2 ms).
SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS repair_observation_scopes (
    scope       text NOT NULL,
    environment text NOT NULL,
    source      text NOT NULL,
    cursor      text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, environment, source)
);

COMMENT ON TABLE repair_observation_scopes IS
    '#6477 independent scan position of alerts carrying no project, observed under the declared mesh scope; the journal stream keeps its own cursor.';
COMMENT ON COLUMN repair_observation_scopes.scope IS
    '#6477 declared scope (the mesh project read from the configuration), never a built-in name.';
