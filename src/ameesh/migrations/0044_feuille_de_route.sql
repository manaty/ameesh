-- SPDX-License-Identifier: AGPL-3.0-only
-- 0044_feuille_de_route — dates prévues et engagements datés (lot L96).
--
-- Le suivi de projet appartient au cœur générique (étude « cœur générique et
-- modules métier ») : rien ici ne nomme un état ou un jalon d'un métier.
--
-- 1. Dates PRÉVUES d'une tâche (`work_items`) : début, fin, livraison (ou
--    déploiement), avec leur source (`ameesh work plan <id> --debut … --fin …
--    --livraison … --source …`), qui les a posées et quand. Les dates RÉELLES
--    ne sont pas recopiées : elles se lisent dans le journal et les jalons
--    déjà mesurés.
--
-- 2. Dates d'une fiche `WorkPackage` (epic, jalon, lot du plan) :
--    * `start_on`, `end_on`, `delivery_on` : déclarées AU CANON (clés `start`,
--      `end`, `delivery` ou `date`), recopiées par `canon sync` ;
--    * `planned_*` : posées dans ameesh (`ameesh work plan <fiche> …`) ; elles
--      priment sur celles du canon, et la vue dit d'où vient chaque date.
--
-- 3. `commitments` : les engagements datés (« on fera ça lundi ») — quoi, pour
--    quand, porteur, projet, tâche ou fiche rattachée, et source (conversation,
--    décision du canon, message du fil, tâche, autre) avec son identifiant.
--    `kind = 'decision'` : un jalon de décision (question ouverte qui attend un
--    humain), sa date peut manquer. `status = 'proposed'` : une proposition
--    tirée des décisions ou du corps d'une tâche, à valider (`ameesh plan
--    accept`) — jamais une création sans contrôle. `proposal_key` dédoublonne
--    les propositions enregistrées.
--
-- 4. Index de `agent_mailbox (sender, id)` : la dernière avancée d'un agent
--    (« qui avance sur quoi ») se lit sans parcourir la boîte.

alter table work_items
    add column if not exists planned_start    date,
    add column if not exists planned_end      date,
    add column if not exists planned_delivery date,
    add column if not exists planned_source   text,
    add column if not exists planned_by       text,
    add column if not exists planned_at       timestamptz;

alter table work_items drop constraint if exists work_items_planned_order_check;
alter table work_items add constraint work_items_planned_order_check
    check (planned_start is null or planned_end is null or planned_start <= planned_end);

alter table work_packages
    add column if not exists start_on         date,
    add column if not exists end_on           date,
    add column if not exists delivery_on      date,
    add column if not exists planned_start    date,
    add column if not exists planned_end      date,
    add column if not exists planned_delivery date,
    add column if not exists planned_source   text,
    add column if not exists planned_by       text,
    add column if not exists planned_at       timestamptz;

create table if not exists commitments (
    id           bigserial primary key,
    what         text not null check (length(btrim(what)) > 0),
    due_on       date,
    kind         text not null default 'commitment'
                 check (kind in ('commitment', 'decision')),
    status       text not null default 'open'
                 check (status in ('proposed', 'open', 'done', 'cancelled')),
    owner        text,
    project      text,
    work_item_id bigint references work_items(id) on delete set null,
    package_id   text,
    source_kind  text not null default 'conversation'
                 check (source_kind in ('conversation', 'decision', 'thread', 'task',
                                        'other')),
    source_ref   text,
    depends_on   jsonb not null default '[]'::jsonb,
    note         text not null default '',
    created_by   text not null default '',
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),
    closed_at    timestamptz,
    proposal_key text unique,
    -- un engagement a une date ; seuls un jalon de décision et une
    -- proposition (la date se fixe en la validant) peuvent ne pas en avoir
    constraint commitments_due_check
        check (due_on is not null or kind = 'decision' or status in ('proposed', 'cancelled'))
);

create index if not exists commitments_due_idx
    on commitments (due_on) where status in ('proposed', 'open');
create index if not exists commitments_item_idx
    on commitments (work_item_id) where work_item_id is not null;

comment on table commitments is
  'L96 : engagements datés (quoi, pour quand, porteur, projet, source) et jalons de décision ; status proposed = proposition à valider.';

create index if not exists agent_mailbox_sender_idx on agent_mailbox (sender, id desc);
