-- SPDX-License-Identifier: AGPL-3.0-only
-- #2184/N1 : une pause de drain posée sur un agent `execute` doit encore
-- pouvoir être levée si le mode de l'agent change pendant l'attente (passage
-- à `externe`). 0458 exigeait `target.mode = 'execute'` : la reprise
-- conditionnelle devenait sans effet, et la pause du drain restait orpheline
-- jusqu'à une levée manuelle. Le mode ne protège pas la pause — il ne fait
-- que rendre l'agent hors de portée de l'exécuteur ; l'autorité d'opérateur
-- reste portée par p_actor/p_principal, et l'identité de la pause par
-- p_expect_reason, comparée sous le verrou de ligne comme en 0458.
--
-- La pose (`ameesh_operator_pause`, 0454) garde sa garde `mode = 'execute'` :
-- on ne pose toujours pas de pause sur un agent non-execute ; seule la levée
-- conditionnelle d'une pause déjà posée est élargie.
--
-- SECURITY INVOKER : jamais d'autorité d'opérateur pour un exécutant ; le
-- chemin Python (agent_pause.change) reste le seul à porter l'acteur humain.
CREATE OR REPLACE FUNCTION ameesh_operator_resume_if(p_agent text, p_reason text,
                                                     p_actor text, p_principal text,
                                                     p_expect_reason text)
RETURNS TABLE(agent text, mode text, operator_paused boolean, changed boolean, event_id bigint)
LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
DECLARE
    target agent_registry%ROWTYPE;
    was_changed boolean := false;
    event_number bigint;
BEGIN
    IF p_reason IS NULL OR length(trim(p_reason)) NOT BETWEEN 1 AND 2000
        OR p_expect_reason IS NULL OR length(trim(p_expect_reason)) NOT BETWEEN 1 AND 2000
        OR p_actor IS NULL OR p_actor !~ '^human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$'
        OR p_principal IS NULL OR p_principal !~ '^human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$'
    THEN
        RAISE EXCEPTION 'invalid conditional operator resume request';
    END IF;
    p_reason := trim(p_reason);
    p_expect_reason := trim(p_expect_reason);
    SELECT r.* INTO target FROM agent_registry r WHERE r.name = p_agent FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;
    -- Identité de la pause en cours : sa raison exacte, comparée sous le
    -- verrou. Un remplacement concurrent (levée puis nouvelle pause) attend
    -- la fin de cette instruction et ne peut donc pas être levé ici.
    IF target.operator_paused IS TRUE
        AND target.operator_pause_reason IS NOT DISTINCT FROM p_expect_reason
    THEN
        UPDATE agent_registry r
           SET operator_paused = false,
               operator_pause_reason = NULL,
               operator_pause_by = NULL,
               operator_pause_for = NULL,
               operator_pause_at = NULL,
               updated_at = clock_timestamp()
         WHERE r.name = target.name;
        INSERT INTO operator_pause_events(agent, paused, reason, actor, principal)
            VALUES (target.name, false, p_reason, p_actor, p_principal)
            RETURNING id INTO event_number;
        target.operator_paused := false;
        was_changed := true;
    END IF;
    RETURN QUERY SELECT target.name, target.mode, target.operator_paused, was_changed, event_number;
END;
$$;
REVOKE ALL ON FUNCTION ameesh_operator_resume_if(text, text, text, text, text) FROM PUBLIC;

-- #2184/R3 (B1 « avant création de l'unité ») : barrière côté base pour une
-- pose de pause dont la commande distante (systemd-run) peut encore créer et
-- exécuter son unité après le stop et la dernière lecture du client. Le stop
-- d'une unité absente (rc 5) ne prouve pas son annulation ; seule une échéance
-- absolue, lue sur l'horloge de la base, permet de refuser une pose tardive.
--
-- Le contrôle est lu APRÈS le SELECT ... FOR UPDATE et sous le verrou de ligne
-- conservé jusqu'à l'UPDATE : un contrôle avant le verrou pourrait être vrai,
-- attendre derrière une transaction, puis écrire après l'échéance. La fonction
-- 5-arg de 0454 reste inchangée pour les appelants sans échéance.
CREATE FUNCTION ameesh_operator_pause_before(
    p_agent text, p_paused boolean, p_reason text,
    p_actor text, p_principal text, p_not_after timestamptz)
RETURNS TABLE(agent text, mode text, operator_paused boolean, changed boolean,
              event_id bigint, refusal text)
LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
DECLARE
    target agent_registry%ROWTYPE;
    was_changed boolean := false;
    event_number bigint;
    why text;
BEGIN
    IF p_agent IS NULL
        OR p_paused IS NULL
        OR p_reason IS NULL OR length(trim(p_reason)) NOT BETWEEN 1 AND 2000
        OR p_actor IS NULL OR p_actor !~ '^human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$'
        OR p_principal IS NULL OR p_principal !~ '^human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$'
        OR p_not_after IS NULL
    THEN
        RAISE EXCEPTION 'invalid operator pause request';
    END IF;
    p_reason := trim(p_reason);
    SELECT r.* INTO target FROM agent_registry r WHERE r.name = p_agent FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;
    -- Échéance lue sous le verrou, juste avant l'écriture : une pose qui a
    -- attendu le verrou au-delà de p_not_after est refusée sans événement.
    IF clock_timestamp() > p_not_after THEN
        why := 'not_after';
        RETURN QUERY SELECT target.name, target.mode, target.operator_paused,
                            false, event_number, why;
        RETURN;
    END IF;
    IF target.mode = 'execute' AND target.operator_paused IS DISTINCT FROM p_paused THEN
        UPDATE agent_registry r
           SET operator_paused = p_paused,
               operator_pause_reason = CASE WHEN p_paused THEN p_reason ELSE NULL END,
               operator_pause_by = CASE WHEN p_paused THEN p_actor ELSE NULL END,
               operator_pause_for = CASE WHEN p_paused THEN p_principal ELSE NULL END,
               operator_pause_at = CASE WHEN p_paused THEN clock_timestamp() ELSE NULL END,
               updated_at = clock_timestamp()
         WHERE r.name = target.name;
        INSERT INTO operator_pause_events(agent, paused, reason, actor, principal)
            VALUES (target.name, p_paused, p_reason, p_actor, p_principal)
            RETURNING id INTO event_number;
        target.operator_paused := p_paused;
        was_changed := true;
    END IF;
    RETURN QUERY SELECT target.name, target.mode, target.operator_paused,
                        was_changed, event_number, why;
END;
$$;
REVOKE ALL ON FUNCTION ameesh_operator_pause_before(text, boolean, text, text, text, timestamptz) FROM PUBLIC;
