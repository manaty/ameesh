-- SPDX-License-Identifier: AGPL-3.0-only
-- 0042_budget_en_base — plafonds de budget réglés en base, pour tout le mesh
-- (lot L70, décision 0019 §2, exigence R20).
--
-- Avant L70, le seul plafond était `budget_usd_per_hour` de la configuration
-- de CHAQUE hôte, lu au démarrage de l'exécuteur : il fallait éditer un
-- fichier par machine et redémarrer. Désormais `ameesh budget set` écrit ici ;
-- tous les exécuteurs du mesh relisent ces lignes à chaud (cache court, et
-- réveil sur le canal `ameesh_budget`). La configuration de l'hôte reste le
-- défaut quand la base ne dit rien.
--
-- `budget_limits` : une ligne par (portée, fenêtre).
--   * `scope` : '' = tout le mesh (somme des agents payés au token), sinon
--     le nom d'un agent (sa propre dépense payée au token) ;
--   * `window_s` : 3600 (plafond horaire) ou 86400 (plafond par jour), en
--     fenêtre glissante ;
--   * `usd` : strictement positif (désactiver la garde reste un choix local,
--     `budget_usd_per_hour: 0` de la configuration de l'hôte).
--
-- `budget_events` : journal append-only des changements (acteur, ancienne
-- et nouvelle valeur ; NULL = absente, c'est-à-dire config ou défaut).
--
-- Le numéro 0042 laisse libres 0037–0041, visés par des lots parallèles
-- (sessions filles, rotation) : les migrations s'appliquent par version
-- manquante, un trou n'est pas une erreur.

create table if not exists budget_limits (
    scope      text not null default '',
    window_s   integer not null check (window_s in (3600, 86400)),
    usd        double precision not null check (usd > 0),
    set_by     text not null,
    updated_at timestamptz not null default now(),
    primary key (scope, window_s)
);

create table if not exists budget_events (
    id       bigserial primary key,
    scope    text not null default '',
    window_s integer not null,
    old_usd  double precision,
    new_usd  double precision,
    actor    text not null,
    at       timestamptz not null default now()
);

create index if not exists budget_events_at on budget_events (at desc);

-- Réveil des exécuteurs : un changement de plafond invalide leur cache et
-- relance la garde des agents en pause (le signal n'est qu'un réveil ; la
-- valeur fait foi dans la table).
create or replace function ameesh_budget_notify() returns trigger
language plpgsql as $$
begin
    perform pg_notify('ameesh_budget', json_build_object(
        'scope', coalesce(new.scope, old.scope),
        'window_s', coalesce(new.window_s, old.window_s))::text);
    return null;
end $$;

drop trigger if exists budget_limits_notify on budget_limits;
create trigger budget_limits_notify
    after insert or update or delete on budget_limits
    for each row execute function ameesh_budget_notify();
