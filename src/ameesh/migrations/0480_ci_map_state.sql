-- SPDX-License-Identifier: AGPL-3.0-only
-- #3344 (panier N de #3343) : l'âge de la carte de mesure, vu d'ameesh.
--
-- Une carte de plus de 48 h fait retomber chaque CI de lot sur la suite
-- complète (`scripts/test-plan.py`, MAX_AGE_H). Le plan le dit en annotation
-- CI, mais rien ne l'alertait : la carte du 17/10 est restée seule sept jours.
-- Le relevé de la CI (`ci.Watcher`, `ameesh notify` sur `github.host`) lit au
-- plus une fois par heure la dernière exécution réussie de `carte.yml` sur la
-- base du dépôt et l'écrit ici ; `ameesh alerts` en tire `map_stale` sans
-- appel GitHub. `published_at` NULL : aucune exécution réussie trouvée.
-- Additive : une table neuve, rien d'existant n'est touché.
set local lock_timeout = '5s';

create table if not exists ci_map_state (
    repo         text not null primary key,
    -- branche de base lue (la carte de `main` est celle des plans de lot)
    branch       text not null,
    -- dernière exécution réussie de carte.yml sur cette branche, et sa tête
    run_id       bigint,
    head_sha     text,
    -- fin de cette exécution (`updated_at` GitHub) : la carte est publiée
    -- par son dernier job, quelques minutes après sa date `genere`
    published_at timestamptz,
    seen_at      timestamptz not null default now()
);
