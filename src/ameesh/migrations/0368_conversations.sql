-- SPDX-License-Identifier: AGPL-3.0-only
-- L78: per-agent conversation cursor; row locking keeps commit order.
CREATE TABLE IF NOT EXISTS conversation_heads (
    agent text PRIMARY KEY CHECK (length(agent) BETWEEN 1 AND 256),
    seq bigint NOT NULL DEFAULT 0 CHECK (seq >= 0)
);
CREATE TABLE IF NOT EXISTS conversation_log (
    agent text NOT NULL REFERENCES conversation_heads(agent),
    seq bigint NOT NULL CHECK (seq > 0),
    session text CHECK (length(session) <= 256),
    turn text CHECK (length(turn) <= 256),
    kind text NOT NULL CHECK (kind IN
        ('human_message', 'input', 'text', 'tool', 'turn_end', 'state')),
    author text NOT NULL CHECK (length(author) BETWEEN 1 AND 256),
    text text NOT NULL CHECK (length(text) <= 20000),
    truncated boolean NOT NULL DEFAULT false,
    meta jsonb NOT NULL DEFAULT '{}',
    event_id text CHECK (length(event_id) <= 256),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    project text NOT NULL CHECK (length(project) BETWEEN 1 AND 256),
    lot text CHECK (length(lot) <= 256),
    -- Durable outbox for R12. Never sent to progress or the chat subscriber.
    thread_text text CHECK (length(thread_text) <= 100000),
    thread_entry text,
    PRIMARY KEY (agent, seq),
    UNIQUE (agent, event_id)
);
CREATE INDEX IF NOT EXISTS conversation_retention ON conversation_log(created_at);
CREATE INDEX IF NOT EXISTS conversation_thread_outbox ON conversation_log(agent, seq)
    WHERE thread_text IS NOT NULL AND thread_entry IS NULL;

CREATE OR REPLACE FUNCTION ameesh_conversation_notify() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('ameesh_chat', json_build_object(
        'agent', NEW.agent, 'seq', NEW.seq)::text);
    RETURN NEW;
END
$$;
DROP TRIGGER IF EXISTS conversation_notify ON conversation_log;
CREATE TRIGGER conversation_notify AFTER INSERT ON conversation_log
    FOR EACH ROW EXECUTE FUNCTION ameesh_conversation_notify();
