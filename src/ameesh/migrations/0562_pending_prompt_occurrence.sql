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
--
-- N-1 (#204724) : les exécuteurs restés sur la version précédente écrivent
-- `pending_prompt` / `current_prompt` sans connaître les deux colonnes. Le
-- déclencheur ci-dessous maintient l'invariant pendant la vague — une
-- consigne en attente ou un tour courant a toujours son occurrence — et
-- rejoue les deux transports que l'ancien code ne fait pas : consigne
-- consommée → tour courant, tour restauré → consigne en attente. Une
-- nouvelle mise en attente de l'ancien code reçoit ainsi une occurrence
-- neuve : deux « continue » successifs ne partagent plus la même clé.
alter table agent_registry add column if not exists pending_prompt_id uuid;
alter table agent_registry add column if not exists current_prompt_id uuid;

update agent_registry set pending_prompt_id = gen_random_uuid()
 where pending_prompt is not null and pending_prompt_id is null;

create or replace function ameesh_agent_prompt_occurrence() returns trigger
language plpgsql as $$
begin
    if tg_op = 'UPDATE' then
        -- N-1 `take_pending_prompt` : la consigne consommée passe du registre
        -- au tour courant sans écrire l'identité ; elle suit le texte.
        if new.pending_prompt is null and old.pending_prompt is not null
           and new.current_prompt is not null
           and new.current_prompt_id is not distinct from old.current_prompt_id then
            new.current_prompt_id := old.pending_prompt_id;
        end if;
        -- N-1 `restore_prompt` (et R5, redémarrage, adoption, reprise) : un
        -- tour inachevé repart en attente, éventuellement joint à une consigne
        -- déjà posée. Le texte change et l'écrivain n'a pas renouvelé
        -- l'identité : celle du tour courant prime, comme dans le code neuf.
        if new.current_prompt is null and old.current_prompt is not null
           and new.pending_prompt is not null
           and new.pending_prompt is distinct from old.pending_prompt
           and new.pending_prompt_id is not distinct from old.pending_prompt_id then
            new.pending_prompt_id := old.current_prompt_id;
        end if;
    end if;
    -- une demande en attente a toujours une occurrence (écrivain N-1)
    if new.pending_prompt is not null and new.pending_prompt_id is null then
        new.pending_prompt_id := gen_random_uuid();
    end if;
    -- un tour courant a toujours son occurrence
    if new.current_prompt is not null and new.current_prompt_id is null then
        new.current_prompt_id := gen_random_uuid();
    end if;
    if new.pending_prompt is null then
        new.pending_prompt_id := null;
    end if;
    if new.current_prompt is null then
        new.current_prompt_id := null;
    end if;
    return new;
end;
$$;

-- Le déclencheur ne change aucune écriture lue par le code d'avant : il ne
-- remplit que les deux colonnes neuves, que ce code ignore, et laisse
-- `pending_prompt` / `current_prompt` exactement tels que l'ancien écrivain
-- les a posés. Un exécuteur d'avant 0562 consomme, restaure et reprend donc
-- comme avant ; seul le nouveau lecteur trouve une occurrence sur sa ligne.
-- ameesh: trigger compatible with previous code
drop trigger if exists agent_registry_prompt_occurrence on agent_registry;
create trigger agent_registry_prompt_occurrence
    before insert or update on agent_registry
    for each row execute function ameesh_agent_prompt_occurrence();
