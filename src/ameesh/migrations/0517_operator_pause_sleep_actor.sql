-- SPDX-License-Identifier: AGPL-3.0-only
-- #4525 : le sommeil « feuille de route épuisée » (file_decoupage._sleep) est
-- un geste automatique et déterministe. Il écrivait actor = principal =
-- human:smichea, attribuant à l'owner une pause qu'il n'avait pas posée. La
-- pose et la levée portent désormais l'acteur machine `ameesh:file-decoupage`
-- et gardent l'owner comme principal humain.
--
-- Seule cette identité machine est admise ; tout autre acteur non humain
-- reste refusé, et le principal doit rester un humain. Les cinq fonctions de
-- pause opérateur sont remplacées ensemble pour que la règle ne diverge pas
-- selon le chemin (pose simple, pose avec échéance, pose avec barrière,
-- reprise conditionnelle, expiration). L'expiration doit accepter cet acteur :
-- sans cela, une pause plafonnée posée par le sommeil ne serait jamais levée
-- à son échéance (#3961).
--
-- Migration additive : mêmes signatures et mêmes types de retour ; seules les
-- gardes d'acteur changent. La contrainte de la table d'audit (0441) portait
-- la même règle humaine : elle est remplacée, sinon l'événement du sommeil
-- serait refusé après la fonction.

ALTER TABLE operator_pause_events
    DROP CONSTRAINT IF EXISTS operator_pause_events_actor_check;
ALTER TABLE operator_pause_events
    ADD CONSTRAINT operator_pause_events_actor_check
    CHECK (actor ~ '^human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$'
           OR actor = 'ameesh:file-decoupage');

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
        OR p_actor IS NULL
        OR (p_actor !~ '^human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$'
            AND p_actor <> 'ameesh:file-decoupage')
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
        OR p_actor IS NULL
        OR (p_actor !~ '^human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$'
            AND p_actor <> 'ameesh:file-decoupage')
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

CREATE OR REPLACE FUNCTION ameesh_operator_pause_before(
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
        OR p_actor IS NULL
        OR (p_actor !~ '^human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$'
            AND p_actor <> 'ameesh:file-decoupage')
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

CREATE OR REPLACE FUNCTION ameesh_operator_pause_until(
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
        OR p_actor IS NULL
        OR (p_actor !~ '^human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$'
            AND p_actor <> 'ameesh:file-decoupage')
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

CREATE OR REPLACE FUNCTION ameesh_operator_pause_expire(p_agent text DEFAULT NULL)
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
           AND (r.operator_pause_by ~ '^human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$'
                OR r.operator_pause_by = 'ameesh:file-decoupage')
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
