-- SPDX-License-Identifier: AGPL-3.0-only
-- 0423_turn_work_item — chaque tour dit le lot qu'il a servi et quand il a
-- commencé (étude harnais, constat 4 ; lot #796).
--
-- Le grand livre savait ce qu'un tour avait coûté, jamais **pour quel lot** :
-- le rattachement se déduisait après coup des messages du tour et de l'état du
-- lot, donc faux dès qu'un tour servait deux lots, qu'un lot changeait de main
-- ou qu'un agent travaillait sur autre chose. Le temps d'agent d'un lot
-- (`work_item_measures.agent_minutes`) en héritait : c'était « le reste » de la
-- chronologie d'états, nuit et attentes comprises.
--
-- 1. `turn_costs.work_item_id` : le lot du tour, écrit par l'exécuteur avec le
--    marqueur comptable — donc AVANT le lancement, et rejoué tel quel par la
--    réparation après panne (un nouvel exécuteur ne perd pas l'attribution).
--    NULL : lot inconnu du tour, ou ligne d'avant cette migration.
-- 2. `turn_costs.started_at` : l'instant où le tour a commencé, à l'horloge de
--    la base (l'instant de pose du marqueur). La durée d'un tour se lit
--    `recorded_at - started_at`, sans dépendre d'un fichier de flux ni d'une
--    date de fichier. `recorded_at` (fin d'écriture) reste la fin : un tour
--    réparé après une panne porte donc une durée majorée, jamais silencieuse.
-- 3. `turn_costs.turn_id` : l'identifiant du tour (celui de
--    `turn_resources.turn_id`), écrit lui aussi avec le marqueur. Il relie la
--    ligne du grand livre aux ressources du tour, donc à l'instant où le tour
--    a réellement pris la main (`turn_resources.started_at`), que le marqueur
--    — posé avant le lancement — précède de quelques secondes. C'est le
--    premier tour attribué au lot qui donne le `started_at` du lot.
-- 4. `spend_pending.work_item_id` et `spend_pending.turn_id` : le marqueur
--    porte le lot et le tour, sinon la réparation d'un tour interrompu les
--    perdrait.
-- 5. `work_item_measures.agent_source` : d'où viennent les minutes d'agent —
--    'turns' (union des tours attribués au lot, mesure de #796) ou 'states'
--    (le reste de la chronologie d'états, mesure d'avant, gardée pour les lots
--    dont aucun tour n'est attribué). NULL : mesure d'avant la colonne.
--
-- Numéro 0423 réservé par le lot #796 (`ameesh work`) : 0422 était d'abord
-- réservé par ce lot, puis laissé au lot #747 (PR #615, écrit sans réservation)
-- pour ne pas invalider ses deux relectures déjà rendues. Deux fichiers de même
-- numéro sont refusés par tests/test_l122_numeros_de_migration.py.

alter table turn_costs add column if not exists work_item_id bigint;
alter table turn_costs add column if not exists started_at timestamptz;
alter table turn_costs add column if not exists turn_id text;
alter table spend_pending add column if not exists work_item_id bigint;
alter table spend_pending add column if not exists turn_id text;
alter table work_item_measures add column if not exists agent_source text;

do $$
begin
    if not exists (select 1 from pg_constraint
                   where conname = 'turn_costs_work_item_fk'
                     and conrelid = 'turn_costs'::regclass) then
        alter table turn_costs add constraint turn_costs_work_item_fk
            foreign key (work_item_id) references work_items(id) on delete set null;
    end if;
    if not exists (select 1 from pg_constraint
                   where conname = 'work_item_measures_agent_source_check'
                     and conrelid = 'work_item_measures'::regclass) then
        alter table work_item_measures add constraint work_item_measures_agent_source_check
            check (agent_source is null or agent_source in ('turns', 'states'));
    end if;
end $$;

-- Le temps d'agent d'un lot se lit par lot puis par date de début (union des
-- tours attribués) ; partiel : les lignes sans lot (relais, appareils) ne
-- gonflent pas l'index.
create index if not exists turn_costs_work_item_idx
    on turn_costs (work_item_id, started_at)
    where work_item_id is not null;

-- Les mesures déjà écrites ont toutes suivi la règle des états : le dire, pour
-- qu'une série ne soit jamais lue sans savoir d'où viennent ses minutes.
update work_item_measures set agent_source = 'states' where agent_source is null;
