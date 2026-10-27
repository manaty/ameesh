-- SPDX-License-Identifier: AGPL-3.0-only
-- #3961 (issue #3014) : une pause opérateur a une échéance. Le 27/10, une
-- maintenance du maillage TPK IMA a posé `pause --operator` sur 62/65 agents ;
-- la reprise conditionnelle n'a pas eu lieu et les agents sont restés gelés
-- 70 min, sans échéance ni signal. L'échéance est portée par le registre et
-- lue sur l'horloge de la base ; à l'échéance, la pause est levée par
-- `ameesh_operator_pause_expire`, avec un événement d'audit au nom de l'humain
-- qui l'avait posée (il a choisi l'échéance : la levée est la sienne).
--
-- Migration additive : une colonne, une colonne d'audit, un index partiel,
-- un déclencheur et deux fonctions. Les fonctions de 0441/0454/0458/0459
-- restent ; une pause posée par elles n'a pas d'échéance (NULL = sans fin,
-- signalée par l'alerte `operator_pause_long`).
ALTER TABLE agent_registry ADD COLUMN operator_pause_until timestamptz;
ALTER TABLE operator_pause_events ADD COLUMN until timestamptz;

CREATE INDEX agent_registry_operator_pause_until_idx
    ON agent_registry (operator_pause_until)
    WHERE operator_paused AND operator_pause_until IS NOT NULL;

-- Toute levée (0441, 0454, 0458/0459, échéance) efface l'échéance : une
-- échéance orpheline ne lèverait rien, mais resterait affichée.
CREATE FUNCTION ameesh_operator_pause_until_clear() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT NEW.operator_paused THEN
        NEW.operator_pause_until := NULL;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER agent_registry_operator_pause_until_clear
    BEFORE UPDATE OF operator_paused ON agent_registry
    FOR EACH ROW EXECUTE FUNCTION ameesh_operator_pause_until_clear();

-- Pose d'une pause avec échéance (`p_until`, NULL = sans fin, choix explicite
-- de l'appelant) et barrière de pose (`p_not_after`, #2184/R3, facultative).
-- Une échéance déjà passée sur l'horloge de la base est refusée sans
-- événement (`refusal = 'until_past'`). Même garde `mode = 'execute'` et même
-- règle « une transition, un événement » que 0454.
CREATE FUNCTION ameesh_operator_pause_until(
    p_agent text, p_reason text, p_actor text, p_principal text,
    p_until timestamptz, p_not_after timestamptz)
RETURNS TABLE(agent text, mode text, operator_paused boolean, changed boolean,
              event_id bigint, refusal text, until_ts double precision)
LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
DECLARE
    target agent_registry%ROWTYPE;
    was_changed boolean := false;
    event_number bigint;
    why text;
BEGIN
    IF p_agent IS NULL
        OR p_reason IS NULL OR length(trim(p_reason)) NOT BETWEEN 1 AND 2000
        OR p_actor IS NULL OR p_actor !~ '^human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$'
        OR p_principal IS NULL OR p_principal !~ '^human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$'
    THEN
        RAISE EXCEPTION 'invalid operator pause request';
    END IF;
    p_reason := trim(p_reason);
    SELECT r.* INTO target FROM agent_registry r WHERE r.name = p_agent FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;
    IF p_not_after IS NOT NULL AND clock_timestamp() > p_not_after THEN
        why := 'not_after';
    ELSIF p_until IS NOT NULL AND p_until <= clock_timestamp() THEN
        why := 'until_past';
    ELSIF target.mode = 'execute' AND NOT target.operator_paused THEN
        UPDATE agent_registry r
           SET operator_paused = true,
               operator_pause_reason = p_reason,
               operator_pause_by = p_actor,
               operator_pause_for = p_principal,
               operator_pause_at = clock_timestamp(),
               operator_pause_until = p_until,
               updated_at = clock_timestamp()
         WHERE r.name = target.name;
        INSERT INTO operator_pause_events(agent, paused, reason, actor, principal, until)
            VALUES (target.name, true, p_reason, p_actor, p_principal, p_until)
            RETURNING id INTO event_number;
        target.operator_paused := true;
        target.operator_pause_until := p_until;
        was_changed := true;
    END IF;
    RETURN QUERY SELECT target.name, target.mode, target.operator_paused, was_changed,
                        event_number, why,
                        CASE WHEN target.operator_paused
                             THEN extract(epoch from target.operator_pause_until)::float8
                        END;
END;
$$;
REVOKE ALL ON FUNCTION ameesh_operator_pause_until(text, text, text, text, timestamptz, timestamptz) FROM PUBLIC;

-- Levée des pauses échues (toutes, ou celle de `p_agent`). Appelée à chaque
-- passe par tout exécuteur natif : deux exécuteurs simultanés ne lèvent
-- jamais deux fois (verrou de ligne, SKIP LOCKED, recontrôle sous verrou).
-- L'événement porte l'acteur et le principal de la pause levée ; une pause
-- héritée sans auteur lisible n'est pas levée (l'alerte la signale).
CREATE FUNCTION ameesh_operator_pause_expire(p_agent text DEFAULT NULL)
RETURNS TABLE(agent text, reason text, by_human text, until_ts double precision,
              event_id bigint)
LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
DECLARE
    target agent_registry%ROWTYPE;
    note text;
    event_number bigint;
BEGIN
    FOR target IN
        SELECT r.* FROM agent_registry r
         WHERE r.operator_paused
           AND r.operator_pause_until IS NOT NULL
           AND r.operator_pause_until <= clock_timestamp()
           AND (p_agent IS NULL OR r.name = p_agent)
           AND r.operator_pause_by ~ '^human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$'
           AND r.operator_pause_for ~ '^human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$'
         ORDER BY r.name
         FOR UPDATE SKIP LOCKED
    LOOP
        note := left('deadline reached ('
                     || to_char(target.operator_pause_until AT TIME ZONE 'UTC',
                                'YYYY-MM-DD"T"HH24:MI"Z"')
                     || '), pause lifted: ' || coalesce(target.operator_pause_reason, '?'),
                     2000);
        UPDATE agent_registry r
           SET operator_paused = false,
               operator_pause_reason = NULL,
               operator_pause_by = NULL,
               operator_pause_for = NULL,
               operator_pause_at = NULL,
               updated_at = clock_timestamp()
         WHERE r.name = target.name;
        INSERT INTO operator_pause_events(agent, paused, reason, actor, principal, until)
            VALUES (target.name, false, note, target.operator_pause_by,
                    target.operator_pause_for, target.operator_pause_until)
            RETURNING id INTO event_number;
        agent := target.name;
        reason := target.operator_pause_reason;
        by_human := target.operator_pause_by;
        until_ts := extract(epoch from target.operator_pause_until)::float8;
        event_id := event_number;
        RETURN NEXT;
    END LOOP;
END;
$$;
REVOKE ALL ON FUNCTION ameesh_operator_pause_expire(text) FROM PUBLIC;
