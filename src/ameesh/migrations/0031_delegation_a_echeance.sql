-- SPDX-License-Identifier: AGPL-3.0-only
-- 0031_delegation_a_echeance — délégation de lot à échéance
-- (lot L40, décision 0030 « pas de travail sans réveil possible », point 5).
--
-- `ameesh work delegate <lot> <agent> --within 30m` confie le lot à un agent
-- réveillable pour une durée bornée. À l'échéance, sans tour du délégué sur ce
-- lot (ni transition, note, jalon, action ou message de sa part), le lot
-- REVIENT au délégant : un orchestrateur ne reste plus des heures sur
-- « attend : travail de X » quand X s'est arrêté.
--
-- 1. Sur `work_items`, la délégation EN COURS (null hors délégation) :
--    * `delegated_by` : le délégant (agent ou `human:…`), à qui le lot revient ;
--    * `delegated_at` : début de la délégation (les preuves de travail du
--      délégué sont comptées à partir de cet instant) ;
--    * `due_at` : l'échéance. Effacée quand la délégation est soldée (le
--      délégué a travaillé : `delegated_by`/`delegated_at` restent, pour
--      l'affichage) ; les trois sont effacées au retour au délégant et par
--      `work assign`.
-- 2. `work_item_delegations` : le registre de chaque délégation et de son
--    issue. Il porte ce que `work_items` ne garde pas : le délégué, le premier
--    tour qu'il a fait sur le lot (`first_turn_at`, noté par l'exécuteur au
--    début du tour), et l'issue (`outcome`) — `soldee` (travail constaté à
--    l'échéance), `rendue` (retour au délégant), `remplacee` (nouvelle
--    délégation), `annulee` (`work assign`, lot fermé, assigné changé). Une
--    ligne sans issue est LA délégation en cours (au plus une par lot) ;
--    l'alerte `delegation_expired` lit les retours récents ici.
--
-- Deux exécuteurs qui traitent la même échéance : le premier verrouille les
-- lignes du lot et de la délégation, pose l'issue sous la condition
-- `outcome is null` ; le second relit après le verrou, ne voit plus de
-- délégation en cours, ne fait rien.

alter table work_items add column if not exists delegated_by text;
alter table work_items add column if not exists delegated_at timestamptz;
alter table work_items add column if not exists due_at timestamptz;

-- l'exécuteur cherche à chaque passe les échéances dépassées : index partiel
create index if not exists work_items_due_idx
    on work_items (due_at) where due_at is not null;

create table if not exists work_item_delegations (
    id             bigserial primary key,
    work_item_id   bigint not null references work_items (id) on delete cascade,
    delegate       text not null,
    delegated_by   text not null,
    delegated_at   timestamptz not null default now(),
    due_at         timestamptz not null,
    first_turn_at  timestamptz,
    outcome        text
                   check (outcome is null
                          or outcome in ('soldee', 'rendue', 'remplacee', 'annulee')),
    resolved_at    timestamptz,
    resolved_by    text,
    check ((outcome is null) = (resolved_at is null)),
    check (delegate <> delegated_by)
);

-- au plus UNE délégation en cours par lot
create unique index if not exists work_item_delegations_open_idx
    on work_item_delegations (work_item_id) where outcome is null;
create index if not exists work_item_delegations_due_idx
    on work_item_delegations (due_at) where outcome is null;
create index if not exists work_item_delegations_returned_idx
    on work_item_delegations (resolved_at) where outcome = 'rendue';
