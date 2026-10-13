-- SPDX-License-Identifier: AGPL-3.0-only
-- L232e: persistent withdrawal, return requests and agent origin tracking.
CREATE TABLE host_lifecycle (
    host text PRIMARY KEY,
    mode text NOT NULL CHECK (mode IN ('draining','returning','available','lost')),
    generation bigint NOT NULL DEFAULT 1 CHECK (generation > 0),
    actor text NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    return_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE host_lifecycle_agents (
    host text NOT NULL REFERENCES host_lifecycle(host),
    generation bigint NOT NULL,
    agent text NOT NULL REFERENCES agent_registry(name),
    critical boolean NOT NULL DEFAULT false,
    operation text,
    missing jsonb NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY(host, generation, agent)
);
CREATE FUNCTION ameesh_host_claims_open(wanted_host text) RETURNS boolean
LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    RETURN NOT EXISTS (SELECT 1 FROM host_lifecycle
                       WHERE host=wanted_host AND mode IN ('draining','lost'));
END $$;
CREATE FUNCTION ameesh_host_gate_state(wanted_host text, observed text) RETURNS text
LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    IF NOT ameesh_host_claims_open(wanted_host) THEN
        RETURN 'draining';
    END IF;
    RETURN observed;
END $$;
