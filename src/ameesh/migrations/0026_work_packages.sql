-- SPDX-License-Identifier: AGPL-3.0-only
-- 0026_work_packages — le plan de travail (lot L29, décision 0005).
--
-- 1. `work_packages` : copie de travail des fiches `WorkPackage` du canon
--    (jalon → epic → lot), écrite par `canon sync` avec leur `canon_ref`
--    (`membre:chemin@commit`). Déclaratif seulement : aucun état d'exécution
--    ici. Une fiche retirée du canon n'est pas effacée (des lots peuvent la
--    référencer) : `present` passe à faux.
--
-- 2. `work_items` reliés au plan : `package_id` (la fiche du lot ou de
--    l'epic) et `package_parent` (son parent, rafraîchi par `canon sync`) ;
--    `pr_ref` (la PR dont la fusion a fermé le lot) ; fermeture explicite
--    (`ameesh work close <id> --abandoned | --superseded-by <id>`) : état
--    terminal `closed` avec `close_reason` et `superseded_by`.
--
-- 3. jalon `closed` (abandon ou remplacement), au plus un par lot.
--
-- Les contraintes `check` de 0004 et 0013 sont remplacées (même nom) pour
-- admettre le nouvel état et le nouveau jalon.

create table if not exists work_packages (
    id          text primary key,
    kind        text not null check (kind in ('milestone', 'epic', 'lot')),
    title       text not null,
    parent      text,
    responsible text,
    team        text,
    scope       jsonb,
    status      text,
    canon_ref   text not null,
    present     boolean not null default true,
    synced_at   timestamptz not null default now()
);

create index if not exists work_packages_parent_idx on work_packages (parent);

comment on table work_packages is
  'L29 : fiches WorkPackage du canon (jalon, epic, lot), recopiées par canon sync avec canon_ref. Déclaratif : l''état d''exécution reste dans work_items.';

alter table work_items
    add column if not exists package_id     text,
    add column if not exists package_parent text,
    add column if not exists pr_ref         text,
    add column if not exists close_reason   text
        check (close_reason in ('abandoned', 'superseded')),
    add column if not exists superseded_by  bigint references work_items(id);

create index if not exists work_items_package_idx
    on work_items (package_id) where package_id is not null;

-- l'état terminal `closed` (abandonné ou remplacé)
alter table work_items drop constraint if exists work_items_state_check;
alter table work_items add constraint work_items_state_check
    check (state in ('intake','build','qa','merged','promoted',
                     'blocked','waiting_human','closed'));
alter table work_items drop constraint if exists work_items_closed_reason_check;
alter table work_items add constraint work_items_closed_reason_check
    check ((state = 'closed') = (close_reason is not null));

-- le jalon `closed`
alter table work_item_milestones drop constraint if exists work_item_milestones_kind_check;
alter table work_item_milestones add constraint work_item_milestones_kind_check
    check (kind in ('requested', 'frozen', 'verdict', 'merged', 'closed'));

create unique index if not exists work_item_milestones_closed_idx
    on work_item_milestones (work_item_id) where kind = 'closed';
