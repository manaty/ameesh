-- SPDX-License-Identifier: AGPL-3.0-only
-- 0562_pending_prompt_occurrence — #6487 (manaty/ameesh#4988) : identité
-- durable d'une occurrence de consigne en attente.
--
-- Le journal A11 des relances automatiques du runner cléait une demande sur
-- le seul texte de la consigne : deux demandes successives de texte identique
-- (« continue ») partageaient la même clé. La seconde, rencontrant la ligne
-- terminale de la première, n'était ni engagée ni achevée — le tour avait
-- bien lieu, mais restait invisible du journal et du rapport U1.
--
-- L'identité est donc posée à la mise en attente (`pending_prompt_id`),
-- transportée avec la consigne au début du tour (`current_prompt_id`), et
-- rendue telle quelle par `restore_prompt` : la restauration ou la reprise de
-- la même demande reste idempotente, une nouvelle mise en attente renouvelle
-- l'identité. Additif seulement : colonnes nullables, aucune ligne existante
-- n'est modifiée, hormis l'attribution d'une occurrence aux consignes déjà en
-- attente (une occurrence, jamais un rejeu).
alter table agent_registry add column if not exists pending_prompt_id uuid;
alter table agent_registry add column if not exists current_prompt_id uuid;

update agent_registry set pending_prompt_id = gen_random_uuid()
 where pending_prompt is not null and pending_prompt_id is null;
