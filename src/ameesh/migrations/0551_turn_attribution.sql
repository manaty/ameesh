-- SPDX-License-Identifier: AGPL-3.0-only
-- 0551_turn_attribution — attribution d'appelant des tours hors lot
-- (manaty/ameesh#4592, lot #6005).
--
-- `turn_costs.work_item_id` dit le lot qu'un tour a servi. Les tours de rôle
-- lancés par un appelant externe (une exécution IMA : builder, QA fixer,
-- agent documentaire, chat) ne servent aucun lot ameesh : leur ligne restait
-- sans aucune attribution (1 034 tours IMA sur 1 034 le 2026-11-05).
--
-- L'appelant fournit une petite attribution JSON à clés allow-listées
-- (`{source, environment, run_type, run_id, repo, issue_number, pr_number,
-- run_kind}`) : elle est enregistrée **à côté** de `work_item_id`, jamais à sa
-- place (un tour dans un lot garde le lot pour autorité). C'est une donnée :
-- elle n'entre dans aucune admission, aucun budget, aucune autorisation.
--
-- Le marqueur comptable (`spend_pending`) la porte aussi : la ligne du grand
-- livre écrite par la réparation d'un tour interrompu — par le même exécuteur
-- ou un autre — garde la même attribution, comme le lot et l'identifiant du
-- tour (0423, #796).
--
-- N-1 : un écrivain d'avant #6005 remplace le marqueur (`ON CONFLICT (agent)`)
-- sans connaître les deux colonnes ajoutées ici. `attribution` survivrait donc
-- à un nouveau tour sans attribut et serait héritée par la réparation.
-- `attribution_at` est l'instant de pose de l'attribution : la lecture ne la
-- rend que si `attribution_at = created_at` (l'identité du marqueur). Un ancien
-- écrivain change `created_at` sans `attribution_at` : la paire devient
-- périmée et la nouvelle lecture rend NULL.
--
-- Migration additive : les lignes antérieures restent NULL (aucun backfill),
-- et les nouvelles colonnes sont absentes des vues d'avant.
alter table turn_costs add column if not exists attribution jsonb;
alter table spend_pending add column if not exists attribution jsonb;
alter table spend_pending add column if not exists attribution_at timestamptz;

comment on column turn_costs.attribution is
    'manaty/ameesh#4592 : attribution d''appelant du tour hors lot (JSON borné, clés allow-listées), donnée seule, jamais autorité';
comment on column spend_pending.attribution is
    'manaty/ameesh#4592 : attribution d''appelant portée par le marqueur comptable, pour que la réparation écrive la même ligne';
comment on column spend_pending.attribution_at is
    'manaty/ameesh#4592 : instant de pose de l''attribution ; elle n''est lue que si attribution_at = created_at (identité du marqueur), pour qu''un écrivain N-1 qui remplace le marqueur ne la transmette pas au tour suivant';
