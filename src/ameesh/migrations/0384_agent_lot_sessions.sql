-- SPDX-License-Identifier: AGPL-3.0-only
-- L165b (#408): an idle agent never waits on a review.
--   * agent_lot_sessions: one row per (agent, lot). The registry keeps the
--     ACTIVE session (session_id, session_work_item); this table keeps the
--     sessions put aside for the agent's other lots (session_id, and the
--     harness, account and cwd it can only be resumed under), and the
--     runner's wake bookkeeping for the lot (rule 1): wakes since the last
--     activity seen on the lot, and when. Rows of merged or closed lots, or of
--     lots no longer assigned to the agent, are purged by the branch sweep.
--   * agent_registry.lot_wake: 'on' | 'off' (rule 1 per agent; NULL = the
--     runner's default, AMEESH_LOT_WAKE).
CREATE TABLE IF NOT EXISTS agent_lot_sessions (
    agent text NOT NULL,
    work_item_id bigint NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
    session_id text,
    harness text,
    account text,
    cwd text,
    parked_at timestamptz,
    wakes integer NOT NULL DEFAULT 0,
    last_wake_at timestamptz,
    wake_activity_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (agent, work_item_id)
);
CREATE INDEX IF NOT EXISTS agent_lot_sessions_lot ON agent_lot_sessions(work_item_id);

ALTER TABLE agent_registry ADD COLUMN IF NOT EXISTS lot_wake text
    CHECK (lot_wake IS NULL OR lot_wake IN ('on', 'off'));
