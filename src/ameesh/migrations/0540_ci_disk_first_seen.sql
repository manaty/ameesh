-- SPDX-License-Identifier: AGPL-3.0-only
-- #5633 : première apparition d'une VM CI, distincte de la dernière (seen_at).
-- La grâce initiale lit first_seen_at (stable) ; l'oubli lit seen_at (rafraîchi
-- à chaque job vu, y compris sans mesure). Migration additive.
set local lock_timeout = '5s';
alter table ci_disk_state
    add column if not exists first_seen_at timestamptz not null default now();
-- Les lignes antérieures n'ont pas de première apparition connue : la dernière
-- vue (seen_at) en est une borne basse sûre, jamais postérieure à la grâce.
update ci_disk_state set first_seen_at = seen_at where first_seen_at > seen_at;
