-- SPDX-License-Identifier: AGPL-3.0-only
-- L232a: durable, reserved moves. No provisioning or secret is stored here.
ALTER TABLE agent_registry ADD COLUMN handoff_fenced boolean NOT NULL DEFAULT false;

CREATE TABLE host_handoffs (
    id text PRIMARY KEY,
    agent text NOT NULL REFERENCES agent_registry(name),
    source text NOT NULL,
    target text NOT NULL CHECK (target <> source),
    state text NOT NULL DEFAULT 'planned' CHECK (state IN
        ('planned','prepared','drained','published','transferred','session_ready',
         'switched','activated','verified','held','cancelled')),
    fenced boolean NOT NULL DEFAULT false,
    plan jsonb NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}',
    memory_bytes bigint NOT NULL CHECK (memory_bytes > 0),
    disk_bytes bigint NOT NULL CHECK (disk_bytes > 0),
    reserved_until timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX host_handoff_one_active ON host_handoffs(agent)
    WHERE state NOT IN ('verified','cancelled');
CREATE INDEX host_handoff_reservations ON host_handoffs(target)
    WHERE state NOT IN ('verified','cancelled');
CREATE TABLE host_handoff_events (
    id bigserial PRIMARY KEY,
    operation text NOT NULL REFERENCES host_handoffs(id),
    state text NOT NULL,
    detail text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE FUNCTION ameesh_handoff_fenced(persona text) RETURNS boolean
LANGUAGE sql VOLATILE AS $$
    SELECT EXISTS (SELECT 1 FROM host_handoffs WHERE agent = persona AND fenced)
$$;
