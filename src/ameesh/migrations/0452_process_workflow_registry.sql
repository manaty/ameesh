-- SPDX-License-Identifier: AGPL-3.0-only
-- 0452_process_workflow_registry — registre natif de révision/étape et reçus
-- de commande transactionnels idempotents (#1922, tranche B de #1915).
--
-- Contrat consommateur : manaty/ameesh#1379 (corps et commentaire 6207021753).
-- Tout est ADDITIF :
--
--   * un lot sans workflow garde `workflow_ref` NULL, `workflow_stage` NULL et
--     `workflow_version = 0` : rien de son comportement ne change ;
--   * l'historique existant de `work_item_events` est conservé tel quel (les
--     nouvelles colonnes y sont NULL) ;
--   * `workflow_version` est la **révision Process** admise (l'artefact
--     compilé de la tranche A), pas la version optimiste de transition : celle
--     -ci est l'id de la dernière ligne du journal du lot, lue et comparée
--     sous verrou par `process_registry`.
--
-- Un reçu est une ligne de `work_item_events` portant un `command_id` (UUID) :
-- l'index unique partiel (work_item_id, command_id) fait l'idempotence — deux
-- commandes de même clé ne peuvent pas produire deux reçus — et la contrainte
-- de complétude interdit un reçu sans étape, sans version de Process ni sans
-- preuve structurée. `evidence` porte l'intention canonique (SHA-256 JCS) qui
-- distingue un rejeu d'un conflit de charge.

alter table work_items add column if not exists workflow_ref text;
alter table work_items add column if not exists workflow_stage text;
alter table work_items add column if not exists workflow_version bigint not null default 0;

alter table work_item_events add column if not exists command_id uuid;
alter table work_item_events add column if not exists workflow_stage text;
alter table work_item_events add column if not exists workflow_version bigint;
alter table work_item_events add column if not exists evidence jsonb;

-- L'idempotence native : au plus un reçu par (lot, clé de commande).
create unique index if not exists work_item_events_command_uidx
    on work_item_events (work_item_id, command_id) where command_id is not null;

-- Une version de Process n'est jamais négative.
do $$
begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'work_items'::regclass
                      and conname = 'work_items_workflow_version_nonnegative') then
        alter table work_items add constraint work_items_workflow_version_nonnegative
            check (workflow_version >= 0);
    end if;
    -- Une étape Process sans référence de Process n'a pas de sens : la
    -- référence est ce qui pin la révision admise.
    if not exists (select 1 from pg_constraint
                    where conrelid = 'work_items'::regclass
                      and conname = 'work_items_workflow_stage_needs_ref') then
        alter table work_items add constraint work_items_workflow_stage_needs_ref
            check (workflow_stage is null or workflow_ref is not null);
    end if;
end $$;

-- Un reçu est complet, et sa preuve est un objet à la forme minimale du reçu
-- natif (`process-receipt/1`) : schéma, intention (empreinte SHA-256),
-- commande, autorité et résultat de vérification. Les lignes d'historique
-- (command_id NULL) ne sont pas concernées.
do $$
begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'work_item_events'::regclass
                      and conname = 'work_item_events_receipt_complete') then
        alter table work_item_events add constraint work_item_events_receipt_complete
            check (command_id is null
                   or (workflow_stage is not null
                       and workflow_version is not null
                       and evidence is not null
                       and jsonb_typeof(evidence) = 'object'
                       and evidence->>'schema' = 'process-receipt/1'
                       and jsonb_typeof(evidence->'intent') = 'object'
                       and evidence->'intent'->>'sha256' ~ '^[0-9a-f]{64}$'
                       and jsonb_typeof(evidence->'command') = 'object'
                       and evidence->'command'->>'action' in
                           ('advance', 'hold', 'release', 'transfer')
                       and jsonb_typeof(evidence->'authority') = 'object'
                       and jsonb_typeof(evidence->'verification') = 'object'));
    end if;
end $$;
