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
-- N-1 (#204724, #204850) : les exécuteurs restés sur la version précédente
-- écrivent `pending_prompt` / `current_prompt` sans connaître les deux
-- colonnes. Le déclencheur ci-dessous maintient l'invariant pendant la vague
-- — une consigne en attente ou un tour courant a toujours son occurrence — et
-- rejoue les deux transports que l'ancien code ne fait pas : consigne
-- consommée → tour courant, tour remis en attente → consigne en attente.
-- Toute autre écriture de la file est un remplacement : une occurrence neuve,
-- même quand le texte ne change pas (deux « continue ») ou se termine par
-- l'ancien (« ne pas continue »). La nature remplacement / enrichissement ne
-- se déduit jamais du texte : elle se lit dans les autres colonnes que
-- l'écrivain déplace, ou dans les deux transports ci-dessus.
alter table agent_registry add column if not exists pending_prompt_id uuid;
alter table agent_registry add column if not exists current_prompt_id uuid;

update agent_registry set pending_prompt_id = gen_random_uuid()
 where pending_prompt is not null and pending_prompt_id is null;

create or replace function ameesh_agent_prompt_occurrence() returns trigger
language plpgsql as $$
declare
    -- Un écrivain N-1 qui compose une consigne à partir de l'existante
    -- (déplacement, relève, redémarrage, adoption) déplace aussi une autre
    -- pièce durable de la ligne : hôte, bail, session, brief. `resume` est le
    -- seul à ne changer que la file et `last_seen` (qu'il rafraîchit à chaque
    -- appel, même quand son texte de statut est déjà celui qu'il repose) ;
    -- `set_pending_prompt` seul n'écrit que la file (`pending_prompt`,
    -- `status`, `updated_at`).
    autre_etat boolean := false;
begin
    if tg_op = 'UPDATE' then
        -- N-1 `take_pending_prompt` : la consigne consommée passe du registre
        -- au tour courant sans écrire l'identité ; elle suit le texte.
        if old.pending_prompt is not null and new.pending_prompt is null
           and new.current_prompt is not null
           and new.current_prompt_id is not distinct from old.current_prompt_id then
            new.current_prompt_id := old.pending_prompt_id;
        end if;
        -- N-1 remise en attente du tour inachevé (restore, R5, redémarrage,
        -- adoption, reprise hors bail) : `current_prompt` est effacé et son
        -- texte revient en attente, joint ou non à une consigne déjà posée.
        -- L'identité du tour revient avec lui.
        if old.current_prompt is not null and new.current_prompt is null
           and new.pending_prompt is not null
           and new.pending_prompt is distinct from old.pending_prompt
           and new.pending_prompt_id is not distinct from old.pending_prompt_id then
            new.pending_prompt_id := coalesce(old.current_prompt_id,
                                              old.pending_prompt_id);
        end if;
        -- N-1 écriture de la file : le déclencheur ne se déclenche que si
        -- l'instruction nomme `pending_prompt` ou `current_prompt` (colonne
        -- `update of`). Un remplacement sans changement visible est donc un
        -- vrai remplacement, pas un autre écrivain qui aurait laissé la file
        -- telle quelle.
        if new.pending_prompt is not null
           and new.current_prompt is not distinct from old.current_prompt
           and new.pending_prompt_id is not distinct from old.pending_prompt_id then
            autre_etat := new.host is distinct from old.host
                or new.lease_epoch is distinct from old.lease_epoch
                or new.session_reset_at is distinct from old.session_reset_at
                or new.restart_brief is distinct from old.restart_brief;
            if autre_etat then
                -- Déplacement, relève, redémarrage ou adoption : la consigne
                -- reprise reste la même demande.
                null;
            elsif new.last_seen is distinct from old.last_seen then
                -- `resume` : une consigne reçue sous bail vivant est une
                -- demande neuve (même contrat que le nouveau code) ; hors
                -- bail, elle rejoint la demande reprise. `last_seen` est
                -- rafraîchi à CHAQUE appel de l'ancien `resume`, même quand
                -- `status_text` portait déjà le même texte : deux reprises
                -- successives restent deux reprises.
                if new.pending_prompt is distinct from old.pending_prompt
                   and old.lease_owner is not null
                   and old.lease_expires_at > clock_timestamp() then
                    new.pending_prompt_id := gen_random_uuid();
                end if;
            else
                -- `set_pending_prompt` : remplacement de l'attente, demande
                -- neuve, même à texte identique.
                new.pending_prompt_id := gen_random_uuid();
            end if;
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
    before insert or update of pending_prompt, current_prompt on agent_registry
    for each row execute function ameesh_agent_prompt_occurrence();
