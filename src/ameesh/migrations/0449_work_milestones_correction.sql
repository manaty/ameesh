-- SPDX-License-Identifier: AGPL-3.0-only
-- ameesh: non additive — reprise de données : le DELETE ci-dessous retire des
-- jalons `merged` périmés. Aucune forme additive ne peut retirer ces lignes ;
-- le code d'avant #1693 lit le jalon sans le supposer présent, il ne casse donc
-- pas (L151 : l'exception est déclarée, pas subie).
-- 0449_work_milestones_correction — #1693 (N4) : une fusion annulée ne date pas.
--
-- Depuis #1693, la correction `merged → build` retire le jalon `merged` du lot
-- dans la transaction de la transition (storage/postgres/work.py) : une fusion
-- annulée ne doit ni dater la livraison (affichage, délais, mesure L157c) ni
-- rester la seule trace d'une fusion — le journal garde la fusion et la
-- correction, et la vraie fusion écrit son propre jalon.
--
-- Les lots corrigés AVANT ce correctif gardent ce jalon. Cette reprise le
-- retire avec un critère étroit : le jalon est encore celui d'une fusion
-- ANNULÉE, c'est-à-dire que la dernière correction `merged → build` du lot est
-- plus récente que sa dernière entrée en `merged` (vue cx2). Un lot refusionné
-- depuis — même ensuite passé à `blocked` ou `waiting_human` — garde donc son
-- jalon, celui de la vraie fusion ; un lot sorti de `merged` par `blocked` ou
-- `waiting_human` sans correction le garde aussi. Seule la fusion que rien
-- n'est venu confirmer disparaît.
delete from work_item_milestones m
 where m.kind = 'merged'
   and (select max(e.created_at) from work_item_events e
         where e.work_item_id = m.work_item_id
           and e.state = 'build'
           and e.note like 'correction: merged → build%')
     > (select max(e.created_at) from work_item_events e
         where e.work_item_id = m.work_item_id
           and e.state = 'merged');
