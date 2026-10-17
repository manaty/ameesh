-- SPDX-License-Identifier: AGPL-3.0-only
-- L87b (#1462), N2 de la R2 qa-01 (#1533) : la validation de 0441 portait sur
-- trim(p_reason) mais la valeur brute était stockée ; un appel SQL direct
-- pouvait donc écrire une raison avec des espaces de bord, alors que le chemin
-- Python (agent_pause.change) strippe déjà. On stocke la valeur normalisée,
-- dans le registre comme dans le journal. 0441 reste immuable (déployée).
CREATE OR REPLACE FUNCTION ameesh_operator_pause(p_agent text, p_paused boolean, p_reason text,
                                                 p_actor text, p_principal text)
RETURNS TABLE(agent text, mode text, operator_paused boolean, changed boolean, event_id bigint)
LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
DECLARE
    target agent_registry%ROWTYPE;
    was_changed boolean := false;
    event_number bigint;
BEGIN
    IF p_paused IS NULL OR p_reason IS NULL OR length(trim(p_reason)) NOT BETWEEN 1 AND 2000
        OR p_actor IS NULL OR p_actor !~ '^human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$'
        OR p_principal IS NULL OR p_principal !~ '^human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$'
    THEN
        RAISE EXCEPTION 'invalid operator pause request';
    END IF;
    p_reason := trim(p_reason);
    SELECT r.* INTO target FROM agent_registry r WHERE r.name = p_agent FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;
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
    RETURN QUERY SELECT target.name, target.mode, target.operator_paused, was_changed, event_number;
END;
$$;
REVOKE ALL ON FUNCTION ameesh_operator_pause(text, boolean, text, text, text) FROM PUBLIC;
