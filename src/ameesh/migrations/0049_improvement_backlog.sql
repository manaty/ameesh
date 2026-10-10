-- SPDX-License-Identifier: AGPL-3.0-only
-- 0049_improvement_backlog — la file d'amélioration continue (lot L119,
-- décision 0037 « ameesh ne s'arrête jamais, il s'améliore »).
--
-- Pas de table neuve : un élément de la file est un lot (`work_items`) de
-- type `improvement`, sans assigné, en `intake`. Ce qu'il porte en plus :
--   * `expected_value` : la valeur attendue, en une phrase (obligatoire : pas
--     de travail pour occuper) ;
--   * `value_score` : la même valeur chiffrée de 1 à 100, qui ordonne la
--     prise automatique (la plus forte d'abord) ;
--   * `priority` : 1 (haute), 2 (normale), 3 (basse) — un humain qui veut
--     passer un élément devant le dit ici, la valeur ne bouge pas ;
--   * `team` : l'équipe qui peut le prendre (NULL : toute équipe) ;
--   * `required_capabilities` : capacités de la fiche Agent exigées (NULL ou
--     vide : aucune).
-- La source de l'élément (constat de l'auditeur, test instable, dette, lot
-- décidé, alerte récurrente) va dans la colonne `source` existante.

alter table work_items drop constraint if exists work_items_type_check;
alter table work_items add constraint work_items_type_check
    check (type in ('bug','evolution','improvement'));

alter table work_items
    add column if not exists expected_value        text,
    add column if not exists value_score           integer
        check (value_score is null or value_score between 1 and 100),
    add column if not exists priority              smallint
        check (priority is null or priority between 1 and 3),
    add column if not exists team                  text,
    add column if not exists required_capabilities text[];

alter table work_items drop constraint if exists work_items_improvement_value_check;
alter table work_items add constraint work_items_improvement_value_check
    check (type <> 'improvement'
           or (coalesce(btrim(expected_value), '') <> '' and value_score is not null));

-- la file : éléments ouverts sans assigné, dans l'ordre de prise
create index if not exists work_items_backlog_idx
    on work_items (coalesce(priority, 2), value_score desc, id)
    where type = 'improvement' and assignee is null and state = 'intake';
