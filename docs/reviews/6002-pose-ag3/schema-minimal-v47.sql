-- #6002 v4.7 — schéma minimal du mesh pour le témoin (relations et colonnes que role-vm-probe.sql et
-- _contrat-vm-probe.sql de M' lisent ou accordent). Banc jetable seulement.
CREATE TABLE host_lifecycle (host text PRIMARY KEY, mode text, claims_open boolean, generation bigint, release_epoch bigint, return_probe text);
CREATE TABLE agent_registry (name text PRIMARY KEY, aliases text[], host text, mode text, retired_at timestamptz, handoff_fenced boolean,
  runner_seen_host text, runner_seen_owner text, runner_seen_at timestamptz, lease_owner text, lease_expires_at timestamptz, status text, last_error text);
CREATE TABLE vm_adapter_requests (host text, generation bigint, release_epoch bigint, state text, status text, result text, updated_at timestamptz);
CREATE TABLE agent_mailbox (id bigserial PRIMARY KEY, sender text, recipient text, body text, host text, kind text,
  created_at timestamptz DEFAULT now(), delivered_at timestamptz, meta jsonb, payload jsonb);
CREATE TABLE turn_resources (turn_id text, agent text, host text, status text, started_at timestamptz, ended_at timestamptz);
CREATE TABLE host_lifecycle_agents (host text, generation bigint, agent text);
CREATE TABLE host_handoffs (source text, target text, state text);
CREATE TABLE work_items (id bigserial PRIMARY KEY, title text);
