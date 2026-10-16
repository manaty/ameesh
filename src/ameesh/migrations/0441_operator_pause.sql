-- SPDX-License-Identifier: AGPL-3.0-only
-- L87b (#1462): independent of leases, budget holds and the active turn.
ALTER TABLE agent_registry
    ADD COLUMN operator_paused boolean NOT NULL DEFAULT false,
    ADD COLUMN operator_pause_reason text,
    ADD COLUMN operator_pause_by text,
    ADD COLUMN operator_pause_for text,
    ADD COLUMN operator_pause_at timestamptz;

CREATE TABLE operator_pause_events (
    id bigserial PRIMARY KEY,
    agent text NOT NULL REFERENCES agent_registry(name),
    paused boolean NOT NULL,
    reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 2000),
    actor text NOT NULL CHECK (actor ~ '^human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$'),
    principal text NOT NULL CHECK (principal ~ '^human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX operator_pause_events_agent ON operator_pause_events(agent, id);

-- Wake the owning runner on resume, including when mail arrived while held.
CREATE TRIGGER agent_registry_operator_pause_notify
    AFTER UPDATE OF operator_paused ON agent_registry
    FOR EACH ROW WHEN (OLD.operator_paused IS DISTINCT FROM NEW.operator_paused)
    EXECUTE FUNCTION agent_mesh_notify_lease();

-- One statement for both PostgreSQL drivers (psql wraps query results in JSON).
-- SECURITY INVOKER: never grant an executor authority to act as an operator.
CREATE FUNCTION ameesh_operator_pause(p_agent text, p_paused boolean, p_reason text,
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
