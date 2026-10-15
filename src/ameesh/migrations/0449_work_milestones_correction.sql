-- SPDX-License-Identifier: AGPL-3.0-only
-- 0449_work_milestones_correction — #1693 (N4) : une fusion annulée ne date pas.
--
-- Depuis #1693, la correction `merged → build` retire le jalon `merged` du lot
-- dans la transaction de la transition (storage/postgres/work.py) : une fusion
-- annulée ne doit ni dater la livraison (affichage, délais, mesure L157c) ni
-- rester la seule trace d'une fusion — le journal garde la fusion et la
-- correction, et la vraie fusion écrit son propre jalon.
--
-- Les lots corrigés AVANT ce correctif gardent ce jalon. Cette reprise le
-- retire, avec un critère étroit : le lot est encore ouvert (ni `merged`, ni
-- `promoted`, ni `closed`) ET son journal porte une correction
-- `merged → build` (la seule transition de `merged` vers `build`, cf.
-- `CORRECTIONS` de work.py). Un lot sorti de `merged` par `blocked` ou
-- `waiting_human` — transitions normales — garde son jalon : sa fusion n'a
-- pas été annulée.
delete from work_item_milestones m
 where m.kind = 'merged'
   and exists (select 1 from work_items w
                where w.id = m.work_item_id
                  and w.state not in ('merged', 'promoted', 'closed'))
   and exists (select 1 from work_item_events e
                where e.work_item_id = m.work_item_id
                  and e.state = 'build'
                  and e.note like 'correction: merged → build%');
