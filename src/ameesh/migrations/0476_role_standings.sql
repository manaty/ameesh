-- SPDX-License-Identifier: AGPL-3.0-only
-- 0476_role_standings — L165 (EP2) : le score des candidats d'un rôle élu.
--
-- Étude docs/design/etudes/election-pilote.md §2-§3. Chaque hôte candidat
-- publie périodiquement, pour chaque rôle `releve: election` auquel il est
-- candidat, son relevé de candidature : disponibilité sur 7 jours (lue dans
-- `host_resources`), préparation (porte de l'hôte, canon synchronisé,
-- compte/harnais disponibles), capacité (départage), et le score qui en
-- découle. HISTORIQUE (append-only) : l'hystérésis lit une heure de relevés
-- pour vérifier qu'un challenger est resté ≥ 5 points au-dessus du détenteur.
--
-- La table n'est jamais lue par l'exécuteur médié : elle vit dans la base du
-- mesh, lue par les exécuteurs des hôtes candidats et par `ameesh role
-- status`. Rétention courte (2 jours), élaguée par l'exécuteur.
--
-- `ready=false` est un relevé à part entière : un hôte muet ou en erreur est
-- écarté par la fraîcheur (`evaluated_at`), jamais deviné.

create table if not exists role_standings (
    id           bigserial primary key,
    name         text not null references agent_registry(name) on delete cascade,
    host         text not null,
    evaluated_at timestamptz not null default now(),
    -- disponibilité sur 7 jours : part du temps couverte par un relevé frais
    availability double precision not null check (availability between 0 and 100),
    -- capacité : marge sous les limites de l'hôte, 0..1, départage seul
    capacity     double precision not null check (capacity between 0 and 1),
    -- score = disponibilité + capacité (< 1 point) ; l'hystérésis, elle,
    -- compare la seule disponibilité : la capacité ne peut pas la franchir
    score        double precision not null,
    ready        boolean not null,
    diagnostic   text not null default ''
);

comment on table role_standings is
    'L165 (EP2): per-candidate-host election score history (7-day availability, readiness, capacity tie-break).';

create index if not exists role_standings_role_time_idx
    on role_standings (name, host, evaluated_at desc, id desc);
create index if not exists role_standings_time_idx
    on role_standings (evaluated_at);
