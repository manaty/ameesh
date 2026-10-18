-- SPDX-License-Identifier: AGPL-3.0-only
-- #1349: one bounded, redacted observation per agent, overwritten by its live runner.
CREATE TABLE IF NOT EXISTS agent_peek (
    agent text PRIMARY KEY REFERENCES agent_registry(name) ON DELETE CASCADE,
    host text NOT NULL,
    lease_epoch bigint NOT NULL,
    captured_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'
                                 AND octet_length(payload::text) <= 131072)
);
