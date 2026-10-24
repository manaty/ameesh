-- SPDX-License-Identifier: AGPL-3.0-only
-- 0478_role_handover — L165 (EP2) : l'état durable d'une bascule planifiée.
--
-- Le détenteur d'un rôle élu rend le bail en fin de tour après avoir noté le
-- challenger retenu : il ne doit pas le reprendre avant le délai de rang du
-- challenger plus une marge (`HANDOVER_RECLAIM_MARGIN_S`). Cette marque vivait
-- en mémoire : un redémarrage de l'exécuteur la perdait, et le détenteur
-- repris pouvait réclamer aussitôt puis redonner le rôle au même challenger
-- mort (N de la relecture #3011, panier #3099).
--
-- La table ne porte que la marque de libération, côté hôte détenteur (un
-- écrivain par hôte) ; le refroidissement d'un challenger qui n'a pas réclamé
-- se déduit de `released_until + HYSTERESIS_S` à la relecture, sans seconde
-- écriture. Les lignes sont élaguées après deux jours, comme les relevés.
--
-- Additive seulement : aucune donnée d'agent n'est déplacée ni supprimée.

create table if not exists role_handover (
    host           text        not null,
    name           text        not null references agent_registry(name) on delete cascade,
    challenger     text        not null,
    released_until timestamptz not null,
    updated_at     timestamptz not null default now(),
    primary key (host, name, challenger)
);

comment on table role_handover is
    'L165 (EP2): holder-side release mark of a planned elected-role handover, durable across runner restarts.';

create index if not exists role_handover_host_idx
    on role_handover (host, name);
create index if not exists role_handover_time_idx
    on role_handover (released_until);
